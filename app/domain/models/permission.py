"""Flight permission models (Rule 2).

These describe what the compliance provider (a Digital Sky style authority) tells us. The
platform never invents a permission: it only stores what the provider granted, and it
re-checks that grant with the provider immediately before a flight starts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.exceptions import PermissionValidationError
from app.domain.models.mission import Mission
from app.domain.models.order import Location

MAX_REASON_LENGTH = 500


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PermissionValidationError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FlightPermission:
    """A permission the provider granted for one specific flight."""

    permission_id: str
    issuer: str
    flight_id: UUID
    drone_id: UUID
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        if not self.permission_id.strip():
            raise PermissionValidationError("permission_id must not be blank")
        if not self.issuer.strip():
            raise PermissionValidationError("issuer must not be blank")
        for name, value in (
            ("issued_at", self.issued_at),
            ("valid_from", self.valid_from),
            ("valid_until", self.valid_until),
        ):
            _require_aware(name, value)
        if self.valid_until <= self.valid_from:
            raise PermissionValidationError("valid_until must be after valid_from")


class DenialCode(StrEnum):
    NO_FLY_ZONE = "no_fly_zone"
    AIRSPACE_CLOSED = "airspace_closed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The provider could not be reached. Fail closed: no answer means no permission."""

    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PermissionDenial:
    code: DenialCode
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise PermissionValidationError("a denial needs a reason")
        if len(self.reason) > MAX_REASON_LENGTH:
            raise PermissionValidationError(f"reason is longer than {MAX_REASON_LENGTH} characters")


class PermissionStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN = "unknown"
    """The provider has no record of this permission id."""


@dataclass(frozen=True, slots=True)
class PermissionValidation:
    """The provider's current answer about a permission.

    ``checked_at`` is when the answer was obtained, on the platform's clock. The flight
    state machine refuses answers that are too old.
    """

    permission_id: str
    status: PermissionStatus
    checked_at: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware("checked_at", self.checked_at)
        if self.valid_until is not None:
            _require_aware("valid_until", self.valid_until)
        if self.status is PermissionStatus.VALID and self.valid_until is None:
            raise PermissionValidationError("a valid permission must report when it ends")


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """What the platform asks the provider for: permission to fly this route."""

    flight_id: UUID
    order_id: UUID
    drone_id: UUID
    route: tuple[Location, ...]
    requested_start: datetime
    estimated_duration_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", tuple(self.route))
        if len(self.route) < 2:
            raise PermissionValidationError("a permission request needs a route of 2+ points")
        _require_aware("requested_start", self.requested_start)
        if not math.isfinite(self.estimated_duration_s) or self.estimated_duration_s <= 0:
            raise PermissionValidationError("estimated_duration_s must be a positive number")

    @classmethod
    def from_mission(
        cls,
        *,
        flight_id: UUID,
        order_id: UUID,
        drone_id: UUID,
        mission: Mission,
        requested_start: datetime,
    ) -> PermissionRequest:
        return cls(
            flight_id=flight_id,
            order_id=order_id,
            drone_id=drone_id,
            route=mission.route,
            requested_start=requested_start,
            estimated_duration_s=mission.estimated_duration_s,
        )
