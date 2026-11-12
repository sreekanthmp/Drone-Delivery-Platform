"""Drones in the fleet registry.

A drone's status says what the platform may do with it. Status changes go through
:meth:`Drone.with_status`, which only allows legal edges. Persistence applies a change as
a compare-and-set on the previous status (see ``DroneStore.apply_status_change``).

Battery and position are set when the drone is registered (and by operations) until
telemetry ingestion exists.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self
from uuid import UUID

from app.domain.exceptions import DroneValidationError, InvalidDroneTransitionError
from app.domain.models.order import Location


class DroneStatus(StrEnum):
    AVAILABLE = "available"
    """Idle at its home dock and may be given a delivery."""

    ASSIGNED = "assigned"
    """Reserved for a flight that has not started yet."""

    ON_MISSION = "on_mission"
    RETURNING = "returning"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"


ALLOWED_DRONE_TRANSITIONS: Final[Mapping[DroneStatus, frozenset[DroneStatus]]] = MappingProxyType(
    {
        DroneStatus.AVAILABLE: frozenset(
            {DroneStatus.ASSIGNED, DroneStatus.MAINTENANCE, DroneStatus.OFFLINE}
        ),
        DroneStatus.ASSIGNED: frozenset(
            {
                DroneStatus.AVAILABLE,  # reservation released: the flight never started
                DroneStatus.ON_MISSION,
                DroneStatus.MAINTENANCE,
                DroneStatus.OFFLINE,
            }
        ),
        DroneStatus.ON_MISSION: frozenset(
            {DroneStatus.RETURNING, DroneStatus.AVAILABLE, DroneStatus.OFFLINE}
        ),
        DroneStatus.RETURNING: frozenset({DroneStatus.AVAILABLE, DroneStatus.OFFLINE}),
        DroneStatus.MAINTENANCE: frozenset({DroneStatus.AVAILABLE, DroneStatus.OFFLINE}),
        DroneStatus.OFFLINE: frozenset({DroneStatus.AVAILABLE, DroneStatus.MAINTENANCE}),
    }
)


@dataclass(frozen=True, slots=True)
class Drone:
    id: UUID
    name: str
    status: DroneStatus
    home: Location
    max_payload_kg: float
    max_range_km: float
    battery_percent: float
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise DroneValidationError("drone name must not be blank")
        if not math.isfinite(self.max_payload_kg) or self.max_payload_kg <= 0:
            raise DroneValidationError(
                f"max_payload_kg must be positive, got {self.max_payload_kg}"
            )
        if not math.isfinite(self.max_range_km) or self.max_range_km <= 0:
            raise DroneValidationError(f"max_range_km must be positive, got {self.max_range_km}")
        if not math.isfinite(self.battery_percent) or not 0 <= self.battery_percent <= 100:
            raise DroneValidationError(
                f"battery_percent must be within [0, 100], got {self.battery_percent}"
            )
        for field_name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise DroneValidationError(f"{field_name} must be timezone-aware")

    @property
    def is_available(self) -> bool:
        return self.status is DroneStatus.AVAILABLE

    def with_status(self, target: DroneStatus, *, now: datetime) -> Self:
        """A copy of this drone in ``target`` status, if that edge is allowed."""
        if target not in ALLOWED_DRONE_TRANSITIONS[self.status]:
            raise InvalidDroneTransitionError(self.status, target)
        return replace(self, status=target, updated_at=now)

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        name: str,
        status: DroneStatus,
        home: Location,
        max_payload_kg: float,
        max_range_km: float,
        battery_percent: float,
        created_at: datetime,
        updated_at: datetime,
    ) -> Self:
        """Rebuild a stored drone in whatever status it is in. For persistence code only."""
        return cls(
            id=id,
            name=name,
            status=status,
            home=home,
            max_payload_kg=max_payload_kg,
            max_range_km=max_range_km,
            battery_percent=battery_percent,
            created_at=created_at,
            updated_at=updated_at,
        )


def check_status_change(before: Drone, after: Drone) -> None:
    """Raise unless ``after`` is ``before`` with only a legal status change applied.

    Both the SQL and the in-memory drone store call this, so a drone's status can only be
    stored if it was derived through :meth:`Drone.with_status`.
    """
    if before.id != after.id:
        raise DroneValidationError("a status change must be about the same drone")
    if after.status not in ALLOWED_DRONE_TRANSITIONS[before.status]:
        raise InvalidDroneTransitionError(before.status, after.status)
    if replace(before, status=after.status, updated_at=after.updated_at) != after:
        raise DroneValidationError("a status change may only change status and updated_at")
