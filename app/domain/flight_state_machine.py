"""The flight state machine, and with it the Rule 2 gate.

Same functional-core shape as :mod:`app.domain.order_state_machine`: ``create`` and
``transition`` never mutate anything. They return a :class:`FlightTransitionResult`
holding the new :class:`Flight` **and** the :class:`AuditEvent` describing the change, so a
flight status change cannot exist without its audit record.

Lifecycle::

    planned -> permission_granted -> mission_uploaded -> started <-> paused
    planned -> permission_denied | cancelled
    permission_granted -> upload_failed | cancelled
    mission_uploaded -> cancelled
    started | paused -> aborted            started -> completed

**The gate (Rule 2).** Entering ``started`` - the first start *and* a resume after a pause -
is the only way a flight can begin or continue, and it requires all of:

* a granted permission and a mission the drone acknowledged (guaranteed by the status),
* a fresh re-check of that permission with the compliance provider (not revoked/unknown),
* the permission still in force right now, and
* enough validity left to cover the whole mission.

If any of that fails a specific :class:`FlightPermissionError` is raised and nothing is
produced. A successful start also yields a :class:`StartAuthorization`, a token that only
this module creates and that the drone link requires before it will send a start command.

Safety commands are deliberately *not* gated: pausing, aborting (return-to-home, land)
and completing never consult the permission, because they must work even when a
permission has expired or been revoked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.exceptions import (
    FlightValidationError,
    InconsistentTransitionResultError,
    InvalidFlightTransitionError,
    MissingTransitionContextError,
    MissionChecksumMismatchError,
    PermissionCheckStaleError,
    PermissionExpiredError,
    PermissionInvalidError,
    PermissionNotGrantedError,
    PermissionRevokedError,
    PermissionWindowTooShortError,
    TerminalFlightStateError,
)
from app.domain.models.audit import AuditEvent, AuditEventType, EventSource
from app.domain.models.flight import Flight, FlightStatus
from app.domain.models.mission import Mission
from app.domain.models.permission import (
    FlightPermission,
    PermissionDenial,
    PermissionStatus,
    PermissionValidation,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]

S = FlightStatus

ALLOWED_FLIGHT_TRANSITIONS: Final[Mapping[FlightStatus, frozenset[FlightStatus]]] = (
    MappingProxyType(
        {
            S.PLANNED: frozenset({S.PERMISSION_GRANTED, S.PERMISSION_DENIED, S.CANCELLED}),
            S.PERMISSION_GRANTED: frozenset({S.MISSION_UPLOADED, S.UPLOAD_FAILED, S.CANCELLED}),
            S.MISSION_UPLOADED: frozenset({S.STARTED, S.CANCELLED}),
            S.STARTED: frozenset({S.PAUSED, S.COMPLETED, S.ABORTED}),
            S.PAUSED: frozenset({S.STARTED, S.ABORTED}),
            S.PERMISSION_DENIED: frozenset(),
            S.UPLOAD_FAILED: frozenset(),
            S.COMPLETED: frozenset(),
            S.ABORTED: frozenset(),
            S.CANCELLED: frozenset(),
        }
    )
)
"""For each flight status, the statuses it may move to."""

DEFAULT_MAX_VALIDATION_AGE: Final = timedelta(seconds=30)
"""How old a compliance re-check may be when a flight starts or resumes."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class StartAuthorization:
    """Proof that the flight state machine allowed this flight to start (Rule 2).

    Only :class:`FlightStateMachine` creates these (an architecture test enforces it), and
    the drone link requires one to send a start command.
    """

    flight_id: UUID
    drone_id: UUID
    permission_id: str
    mission_checksum: str
    authorized_at: datetime
    valid_until: datetime
    """When the permission ends: the latest moment this authorisation can be relied on."""

    def __post_init__(self) -> None:
        if self.valid_until <= self.authorized_at:
            raise FlightValidationError("a start authorisation must be valid after it is issued")


@dataclass(frozen=True, slots=True)
class FlightTransitionResult:
    """The outcome of a successful ``create`` or ``transition``.

    Persistence accepts this as proof that a flight change went through the state machine,
    so it refuses to exist inconsistently: the event must describe exactly this flight, a
    status change must be an allowed edge, and a start authorisation must accompany every
    entry into ``started`` and nothing else.
    """

    flight: Flight
    audit_event: AuditEvent
    start_authorization: StartAuthorization | None = None

    def __post_init__(self) -> None:
        flight, event = self.flight, self.audit_event
        if event.event_type not in {
            AuditEventType.FLIGHT_CREATED,
            AuditEventType.FLIGHT_STATUS_CHANGED,
        }:
            raise InconsistentTransitionResultError("a flight result needs a flight audit event")
        if (
            event.flight_id != flight.id
            or event.order_id != flight.order_id
            or event.drone_id != flight.drone_id
            or event.new_state is not flight.status
        ):
            raise InconsistentTransitionResultError(
                "the audit event does not describe the resulting flight"
            )
        if (
            event.event_type is AuditEventType.FLIGHT_STATUS_CHANGED
            and isinstance(event.previous_state, FlightStatus)
            and isinstance(event.new_state, FlightStatus)
            and event.new_state not in ALLOWED_FLIGHT_TRANSITIONS[event.previous_state]
        ):
            raise InvalidFlightTransitionError(event.previous_state, event.new_state)

        entering_started = (
            event.event_type is AuditEventType.FLIGHT_STATUS_CHANGED
            and event.new_state is FlightStatus.STARTED
        )
        authorization = self.start_authorization
        if entering_started != (authorization is not None):
            raise InconsistentTransitionResultError(
                "a start authorisation must accompany every entry into 'started', and nothing else"
            )
        if authorization is not None and (
            flight.permission is None
            or authorization.flight_id != flight.id
            or authorization.drone_id != flight.drone_id
            or authorization.permission_id != flight.permission.permission_id
            or authorization.mission_checksum != flight.mission.checksum
        ):
            raise InconsistentTransitionResultError(
                "the start authorisation does not match the flight's permission and mission"
            )


class FlightStateMachine:
    """Validates and performs flight lifecycle changes.

    ``clock`` and ``id_factory`` are injectable so tests are deterministic. The machine
    holds no flight state of its own.
    """

    def __init__(
        self,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
        max_validation_age: timedelta = DEFAULT_MAX_VALIDATION_AGE,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._max_validation_age = max_validation_age

    @staticmethod
    def can_transition(current: FlightStatus, target: FlightStatus) -> bool:
        return target in ALLOWED_FLIGHT_TRANSITIONS[current]

    @staticmethod
    def allowed_targets(current: FlightStatus) -> frozenset[FlightStatus]:
        return ALLOWED_FLIGHT_TRANSITIONS[current]

    def create(
        self,
        *,
        order_id: UUID,
        drone_id: UUID,
        mission: Mission,
        source: EventSource,
        metadata: Mapping[str, Any] | None = None,
    ) -> FlightTransitionResult:
        """Plan a new flight (status ``planned``) together with its audit event."""
        now = self._clock()
        flight = Flight(
            id=self._id_factory(),
            order_id=order_id,
            drone_id=drone_id,
            status=S.PLANNED,
            mission=mission,
            created_at=now,
            updated_at=now,
        )
        facts = {
            "mission_id": str(mission.mission_id),
            "mission_checksum": mission.checksum,
            "waypoint_count": len(mission.waypoints),
            "estimated_duration_s": mission.estimated_duration_s,
        }
        event = AuditEvent(
            event_id=self._id_factory(),
            order_id=order_id,
            event_type=AuditEventType.FLIGHT_CREATED,
            previous_state=None,
            new_state=S.PLANNED,
            occurred_at=now,
            source=source,
            drone_id=drone_id,
            flight_id=flight.id,
            metadata={**(metadata or {}), **facts},
        )
        return FlightTransitionResult(flight=flight, audit_event=event)

    def transition(
        self,
        flight: Flight,
        target: FlightStatus,
        *,
        source: EventSource,
        permission: FlightPermission | None = None,
        denial: PermissionDenial | None = None,
        acknowledged_checksum: str | None = None,
        validation: PermissionValidation | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> FlightTransitionResult:
        """Move ``flight`` to ``target``.

        Each target takes exactly the context it needs and rejects any other:

        =====================  =====================================================
        ``permission_granted`` ``permission`` (still in force)
        ``permission_denied``  ``denial``
        ``mission_uploaded``   ``acknowledged_checksum`` (must equal the mission's)
        ``started``            ``validation`` (a fresh compliance re-check: the gate)
        upload_failed,         ``reason``
        cancelled, aborted
        paused, completed      nothing
        =====================  =====================================================

        Raises:
            TerminalFlightStateError: the flight has already finished.
            InvalidFlightTransitionError: the edge is not allowed.
            MissingTransitionContextError / FlightValidationError: context missing/unexpected.
            FlightPermissionError (subclasses): starting or resuming is not permitted.
            MissionChecksumMismatchError: the drone acknowledged a different mission.
        """
        if not self.can_transition(flight.status, target):
            if flight.status.is_terminal:
                raise TerminalFlightStateError(flight.status, target)
            raise InvalidFlightTransitionError(flight.status, target)

        supplied = {
            "permission": permission,
            "denial": denial,
            "acknowledged_checksum": acknowledged_checksum,
            "validation": validation,
            "reason": reason,
        }
        needed = _CONTEXT_FOR[target]
        for name, value in supplied.items():
            if name in needed and value is None:
                raise MissingTransitionContextError(f"moving to '{target}' requires '{name}'")
            if name not in needed and value is not None:
                raise FlightValidationError(f"'{name}' is not accepted when moving to '{target}'")

        now = self._clock()
        updates: dict[str, Any] = {}
        facts: dict[str, Any] = {}
        authorization: StartAuthorization | None = None

        if target is S.PERMISSION_GRANTED:
            assert permission is not None
            if permission.flight_id != flight.id or permission.drone_id != flight.drone_id:
                raise FlightValidationError(
                    "the permission was issued for a different flight or drone"
                )
            if permission.valid_until <= now:
                raise PermissionExpiredError("the granted permission has already expired")
            updates["permission"] = permission
            facts = {
                "permission_id": permission.permission_id,
                "issuer": permission.issuer,
                "valid_from": permission.valid_from.isoformat(),
                "valid_until": permission.valid_until.isoformat(),
            }
        elif target is S.PERMISSION_DENIED:
            assert denial is not None
            updates["denial"] = denial
            facts = {"denial_code": denial.code.value, "denial_reason": denial.reason}
        elif target is S.MISSION_UPLOADED:
            assert acknowledged_checksum is not None
            if acknowledged_checksum != flight.mission.checksum:
                raise MissionChecksumMismatchError(
                    "the drone acknowledged a different mission than the one that was sent"
                )
            updates["acknowledged_checksum"] = acknowledged_checksum
            facts = {"mission_checksum": acknowledged_checksum}
        elif target in {S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED}:
            assert reason is not None
            updates["failure_reason"] = reason
            facts = {"reason": reason}
        elif target is S.STARTED:
            assert validation is not None
            valid_until = self._check_start_gate(flight, validation, now)
            assert flight.permission is not None
            updates["started_at"] = flight.started_at or now
            facts = {
                "permission_id": flight.permission.permission_id,
                "permission_valid_until": valid_until.isoformat(),
                "validated_at": validation.checked_at.isoformat(),
                "resumed": flight.status is S.PAUSED,
            }
            authorization = StartAuthorization(
                flight_id=flight.id,
                drone_id=flight.drone_id,
                permission_id=flight.permission.permission_id,
                mission_checksum=flight.mission.checksum,
                authorized_at=now,
                valid_until=valid_until,
            )

        new_flight = replace(flight, status=target, updated_at=now, **updates)
        event = AuditEvent(
            event_id=self._id_factory(),
            order_id=flight.order_id,
            event_type=AuditEventType.FLIGHT_STATUS_CHANGED,
            previous_state=flight.status,
            new_state=target,
            occurred_at=now,
            source=source,
            drone_id=flight.drone_id,
            flight_id=flight.id,
            metadata={**(metadata or {}), **facts},
        )
        return FlightTransitionResult(
            flight=new_flight, audit_event=event, start_authorization=authorization
        )

    def _check_start_gate(
        self, flight: Flight, validation: PermissionValidation, now: datetime
    ) -> datetime:
        """Rule 2: raise unless this flight may start now. Returns when its permission ends."""
        permission = flight.permission
        if permission is None:
            raise PermissionNotGrantedError("the flight has no granted permission")
        if validation.permission_id != permission.permission_id:
            raise PermissionInvalidError(
                "the compliance check is about a different permission than the flight's"
            )

        age = now - validation.checked_at
        if age < timedelta(0):
            raise PermissionCheckStaleError("the compliance check is dated in the future")
        if age > self._max_validation_age:
            raise PermissionCheckStaleError(
                f"the compliance check is {age.total_seconds():.0f}s old; "
                f"at most {self._max_validation_age.total_seconds():.0f}s is accepted"
            )

        if validation.status is PermissionStatus.REVOKED:
            raise PermissionRevokedError("the compliance provider has revoked the permission")
        if validation.status is PermissionStatus.UNKNOWN:
            raise PermissionInvalidError("the compliance provider does not know this permission")
        if validation.status is PermissionStatus.EXPIRED:
            raise PermissionExpiredError("the compliance provider reports the permission expired")

        # VALID: take the earlier of what we stored and what the provider reports now.
        assert validation.valid_until is not None
        valid_until = min(permission.valid_until, validation.valid_until)
        if now < permission.valid_from:
            raise PermissionInvalidError("the permission is not valid yet")
        if now >= valid_until:
            raise PermissionExpiredError("the permission has expired")
        needed_until = now + timedelta(seconds=flight.mission.estimated_duration_s)
        if valid_until < needed_until:
            raise PermissionWindowTooShortError(
                "the permission ends before the mission would finish "
                f"(valid until {valid_until.isoformat()}, mission needs {needed_until.isoformat()})"
            )
        return valid_until


# The context each target takes (and the only context it accepts).
_CONTEXT_FOR: Final[Mapping[FlightStatus, frozenset[str]]] = MappingProxyType(
    {
        S.PERMISSION_GRANTED: frozenset({"permission"}),
        S.PERMISSION_DENIED: frozenset({"denial"}),
        S.MISSION_UPLOADED: frozenset({"acknowledged_checksum"}),
        S.UPLOAD_FAILED: frozenset({"reason"}),
        S.CANCELLED: frozenset({"reason"}),
        S.ABORTED: frozenset({"reason"}),
        S.STARTED: frozenset({"validation"}),
        S.PAUSED: frozenset(),
        S.COMPLETED: frozenset(),
        S.PLANNED: frozenset(),
    }
)
