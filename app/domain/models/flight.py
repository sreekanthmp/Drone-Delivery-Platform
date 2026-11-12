"""The flight: one drone flying one mission for one order (Rule 2).

A flight walks a lifecycle of its own (see ``app.domain.flight_state_machine``)::

    planned -> permission_granted -> mission_uploaded -> started <-> paused
    planned -> permission_denied | cancelled
    permission_granted -> upload_failed | cancelled
    mission_uploaded -> cancelled
    started | paused -> aborted          started -> completed

Like orders, flights are immutable; only the flight state machine derives a flight with a
different status. The fields that must be present are fixed by the status, so an
impossible flight (``started`` without a permission, say) cannot even be constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Self
from uuid import UUID

from app.domain.exceptions import FlightValidationError
from app.domain.models.mission import Mission
from app.domain.models.permission import FlightPermission, PermissionDenial


class FlightStatus(StrEnum):
    PLANNED = "planned"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_DENIED = "permission_denied"
    MISSION_UPLOADED = "mission_uploaded"
    UPLOAD_FAILED = "upload_failed"
    STARTED = "started"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    CANCELLED = "cancelled"

    @property
    def is_live(self) -> bool:
        return self in LIVE_FLIGHT_STATES

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_FLIGHT_STATES


S = FlightStatus

LIVE_FLIGHT_STATES: Final[frozenset[FlightStatus]] = frozenset(
    {S.PLANNED, S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.PAUSED}
)
"""A drone and an order can each have at most one live flight."""

TERMINAL_FLIGHT_STATES: Final[frozenset[FlightStatus]] = (
    frozenset(FlightStatus) - LIVE_FLIGHT_STATES
)

# Which fields a flight in each status must (or may not) carry.
_PERMISSION_REQUIRED: Final = frozenset(
    {
        S.PERMISSION_GRANTED,
        S.MISSION_UPLOADED,
        S.UPLOAD_FAILED,
        S.STARTED,
        S.PAUSED,
        S.COMPLETED,
        S.ABORTED,
    }
)
_PERMISSION_FORBIDDEN: Final = frozenset({S.PLANNED, S.PERMISSION_DENIED})
_ACK_REQUIRED: Final = frozenset({S.MISSION_UPLOADED, S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED})
_ACK_FORBIDDEN: Final = frozenset(
    {S.PLANNED, S.PERMISSION_GRANTED, S.PERMISSION_DENIED, S.UPLOAD_FAILED}
)
_STARTED_AT_REQUIRED: Final = frozenset({S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED})
_REASON_REQUIRED: Final = frozenset({S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED})


@dataclass(frozen=True, slots=True)
class Flight:
    id: UUID
    order_id: UUID
    drone_id: UUID
    status: FlightStatus
    mission: Mission
    created_at: datetime
    updated_at: datetime
    permission: FlightPermission | None = None
    denial: PermissionDenial | None = None
    acknowledged_checksum: str | None = None
    """The mission checksum the drone echoed back when it accepted the upload."""

    failure_reason: str | None = None
    """Why the flight ended early (upload_failed, cancelled or aborted)."""

    started_at: datetime | None = None
    """When the flight first started; kept across pause and resume."""

    def __post_init__(self) -> None:
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise FlightValidationError(f"{name} must be timezone-aware")
        status = self.status

        if (status in _PERMISSION_REQUIRED) != (self.permission is not None) and (
            status is not S.CANCELLED
        ):
            raise FlightValidationError(
                f"a flight in state '{status}' "
                f"{'requires' if status in _PERMISSION_REQUIRED else 'cannot have'} a permission"
            )
        if self.permission is not None and (
            self.permission.flight_id != self.id or self.permission.drone_id != self.drone_id
        ):
            raise FlightValidationError("the permission was issued for a different flight or drone")

        if (self.denial is not None) != (status is S.PERMISSION_DENIED):
            raise FlightValidationError(
                "only a permission_denied flight carries a denial (and must)"
            )

        if status in _ACK_REQUIRED and self.acknowledged_checksum is None:
            raise FlightValidationError(
                f"a flight in state '{status}' needs the drone's mission ack"
            )
        if status in _ACK_FORBIDDEN and self.acknowledged_checksum is not None:
            raise FlightValidationError(f"a flight in state '{status}' cannot have a mission ack")
        if self.acknowledged_checksum is not None:
            if self.acknowledged_checksum != self.mission.checksum:
                raise FlightValidationError("the acknowledged checksum does not match the mission")
            if self.permission is None:
                raise FlightValidationError("a mission cannot be acknowledged without a permission")

        if (status in _STARTED_AT_REQUIRED) != (self.started_at is not None):
            raise FlightValidationError(
                f"started_at must be set exactly for started, paused, completed and aborted "
                f"flights, not '{status}'"
            )
        if self.started_at is not None and (
            self.started_at.tzinfo is None or self.started_at.utcoffset() is None
        ):
            raise FlightValidationError("started_at must be timezone-aware")

        if (status in _REASON_REQUIRED) != (self.failure_reason is not None):
            raise FlightValidationError(
                "a reason is required exactly for upload_failed, cancelled and aborted "
                f"flights, not '{status}'"
            )
        if self.failure_reason is not None and not self.failure_reason.strip():
            raise FlightValidationError("the reason must not be blank")

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        order_id: UUID,
        drone_id: UUID,
        status: FlightStatus,
        mission: Mission,
        created_at: datetime,
        updated_at: datetime,
        permission: FlightPermission | None = None,
        denial: PermissionDenial | None = None,
        acknowledged_checksum: str | None = None,
        failure_reason: str | None = None,
        started_at: datetime | None = None,
    ) -> Self:
        """Rebuild a stored flight in whatever status it is in. For persistence code only.

        It re-validates every invariant but performs no transition and writes no audit
        event, so it must never be used to change a flight's status.
        """
        return cls(
            id=id,
            order_id=order_id,
            drone_id=drone_id,
            status=status,
            mission=mission,
            created_at=created_at,
            updated_at=updated_at,
            permission=permission,
            denial=denial,
            acknowledged_checksum=acknowledged_checksum,
            failure_reason=failure_reason,
            started_at=started_at,
        )
