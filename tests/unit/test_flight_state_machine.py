"""The flight state machine and the Rule 2 start gate."""

from __future__ import annotations

import itertools
from dataclasses import FrozenInstanceError
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import (
    DomainError,
    FlightPermissionError,
    FlightValidationError,
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
from app.domain.flight_state_machine import (
    ALLOWED_FLIGHT_TRANSITIONS,
    DEFAULT_MAX_VALIDATION_AGE,
    FlightStateMachine,
    StartAuthorization,
)
from app.domain.models import (
    TERMINAL_FLIGHT_STATES,
    AuditEventType,
    DenialCode,
    EventSource,
    Flight,
    FlightStatus,
    PermissionDenial,
    PermissionStatus,
    PermissionValidation,
)
from tests.conftest import (
    DRONE_ID,
    FLIGHT_ID,
    ORDER_ID,
    T0,
    FakeClock,
    SequentialIds,
    build_flight,
    build_mission,
    build_permission,
)

S = FlightStatus

# The spec, written out by hand and deliberately NOT derived from the implementation.
ALLOWED_EDGES: frozenset[tuple[FlightStatus, FlightStatus]] = frozenset(
    {
        (S.PLANNED, S.PERMISSION_GRANTED),
        (S.PLANNED, S.PERMISSION_DENIED),
        (S.PLANNED, S.CANCELLED),
        (S.PERMISSION_GRANTED, S.MISSION_UPLOADED),
        (S.PERMISSION_GRANTED, S.UPLOAD_FAILED),
        (S.PERMISSION_GRANTED, S.CANCELLED),
        (S.MISSION_UPLOADED, S.STARTED),
        (S.MISSION_UPLOADED, S.CANCELLED),
        (S.STARTED, S.PAUSED),
        (S.STARTED, S.COMPLETED),
        (S.STARTED, S.ABORTED),
        (S.PAUSED, S.STARTED),
        (S.PAUSED, S.ABORTED),
    }
)
ALL_PAIRS = list(itertools.product(FlightStatus, FlightStatus))
PAIR_IDS = [f"{src}->{dst}" for src, dst in ALL_PAIRS]

SOURCE_FOR = {
    S.PERMISSION_GRANTED: S.PLANNED,
    S.PERMISSION_DENIED: S.PLANNED,
    S.MISSION_UPLOADED: S.PERMISSION_GRANTED,
    S.UPLOAD_FAILED: S.PERMISSION_GRANTED,
    S.CANCELLED: S.PLANNED,
    S.STARTED: S.MISSION_UPLOADED,
    S.PAUSED: S.STARTED,
    S.COMPLETED: S.STARTED,
    S.ABORTED: S.STARTED,
}
REQUIRED_CONTEXT = {
    S.PERMISSION_GRANTED: "permission",
    S.PERMISSION_DENIED: "denial",
    S.MISSION_UPLOADED: "acknowledged_checksum",
    S.UPLOAD_FAILED: "reason",
    S.CANCELLED: "reason",
    S.ABORTED: "reason",
    S.STARTED: "validation",
}
CONTEXT_NAMES = ["permission", "denial", "acknowledged_checksum", "validation", "reason"]


def good_validation(flight: Flight, now: Any = T0, **overrides: Any) -> PermissionValidation:
    """A fresh VALID answer for the flight's permission (or a plausible one if it has none)."""
    permission = flight.permission
    fields: dict[str, Any] = {
        "permission_id": "PERM-1" if permission is None else permission.permission_id,
        "status": PermissionStatus.VALID,
        "checked_at": now,
        "valid_until": T0 + timedelta(minutes=15) if permission is None else permission.valid_until,
    }
    fields.update(overrides)
    return PermissionValidation(**fields)


def context_for(flight: Flight, target: FlightStatus) -> dict[str, Any]:
    """Valid context for moving ``flight`` to ``target`` at time T0."""
    match target:
        case S.PERMISSION_GRANTED:
            return {"permission": build_permission(flight_id=flight.id, drone_id=flight.drone_id)}
        case S.PERMISSION_DENIED:
            return {"denial": PermissionDenial(DenialCode.NO_FLY_ZONE, "zone")}
        case S.MISSION_UPLOADED:
            return {"acknowledged_checksum": flight.mission.checksum}
        case S.UPLOAD_FAILED | S.CANCELLED | S.ABORTED:
            return {"reason": "because"}
        case S.STARTED:
            return {"validation": good_validation(flight)}
        case _:
            return {}


def sample_value(name: str, flight: Flight) -> Any:
    return {
        "permission": build_permission(flight_id=flight.id, drone_id=flight.drone_id),
        "denial": PermissionDenial(DenialCode.REJECTED, "no"),
        "acknowledged_checksum": flight.mission.checksum,
        "validation": PermissionValidation("PERM-1", PermissionStatus.UNKNOWN, T0),
        "reason": "because",
    }[name]


class TestTransitionTable:
    def test_the_spec_has_13_edges(self) -> None:
        assert len(ALLOWED_EDGES) == 13

    def test_the_table_matches_the_spec_exactly(self) -> None:
        actual = {(s, t) for s, targets in ALLOWED_FLIGHT_TRANSITIONS.items() for t in targets}
        assert actual == ALLOWED_EDGES

    def test_every_status_has_a_row(self) -> None:
        assert set(ALLOWED_FLIGHT_TRANSITIONS) == set(FlightStatus)

    def test_terminal_states_have_no_way_out(self) -> None:
        for status in TERMINAL_FLIGHT_STATES:
            assert ALLOWED_FLIGHT_TRANSITIONS[status] == frozenset()

    def test_a_flight_can_only_be_resumed_from_paused(self) -> None:
        sources = {s for s, t in ALLOWED_EDGES if t is S.STARTED}
        assert sources == {S.MISSION_UPLOADED, S.PAUSED}

    def test_the_table_cannot_be_modified(self) -> None:
        with pytest.raises(TypeError):
            ALLOWED_FLIGHT_TRANSITIONS[S.COMPLETED] = frozenset({S.PLANNED})  # type: ignore[index]
        with pytest.raises(AttributeError):
            ALLOWED_FLIGHT_TRANSITIONS[S.PLANNED].add(S.STARTED)  # type: ignore[attr-defined]

    @pytest.mark.parametrize(("src", "dst"), ALL_PAIRS, ids=PAIR_IDS)
    def test_can_transition_agrees_with_the_spec(
        self, flight_machine: FlightStateMachine, src: FlightStatus, dst: FlightStatus
    ) -> None:
        assert flight_machine.can_transition(src, dst) is ((src, dst) in ALLOWED_EDGES)

    def test_allowed_targets(self, flight_machine: FlightStateMachine) -> None:
        assert flight_machine.allowed_targets(S.STARTED) == {S.PAUSED, S.COMPLETED, S.ABORTED}


class TestTransitionMatrix:
    """Every one of the 100 (from, to) pairs behaves exactly as the spec says."""

    @pytest.mark.parametrize(("src", "dst"), ALL_PAIRS, ids=PAIR_IDS)
    def test_pair(
        self,
        flight_machine: FlightStateMachine,
        ids: SequentialIds,
        src: FlightStatus,
        dst: FlightStatus,
    ) -> None:
        flight = build_flight(src)

        if (src, dst) in ALLOWED_EDGES:
            result = flight_machine.transition(
                flight, dst, source=EventSource.DISPATCH, **context_for(flight, dst)
            )
            assert result.flight.status is dst
            assert result.audit_event.previous_state is src
            assert result.audit_event.new_state is dst
            return

        expected = (
            TerminalFlightStateError
            if src in TERMINAL_FLIGHT_STATES
            else InvalidFlightTransitionError
        )
        issued_before = ids.issued
        with pytest.raises(InvalidFlightTransitionError) as exc_info:
            flight_machine.transition(
                flight, dst, source=EventSource.DISPATCH, **context_for(flight, dst)
            )
        assert type(exc_info.value) is expected
        assert (exc_info.value.current, exc_info.value.target) == (src, dst)
        assert ids.issued == issued_before, "a rejected transition must not mint an audit event"
        assert flight.status is src

    def test_the_exception_hierarchy(self) -> None:
        assert issubclass(TerminalFlightStateError, InvalidFlightTransitionError)
        assert issubclass(InvalidFlightTransitionError, DomainError)
        assert issubclass(FlightPermissionError, DomainError)
        for error in (
            PermissionNotGrantedError,
            PermissionInvalidError,
            PermissionRevokedError,
            PermissionExpiredError,
            PermissionWindowTooShortError,
            PermissionCheckStaleError,
        ):
            assert issubclass(error, FlightPermissionError)

    def test_the_message_names_both_states(self, flight_machine: FlightStateMachine) -> None:
        with pytest.raises(InvalidFlightTransitionError, match=r"planned -> started"):
            flight_machine.transition(
                build_flight(S.PLANNED), S.STARTED, source=EventSource.DISPATCH, validation=None
            )


class TestCreate:
    def test_plans_a_flight_with_its_audit_event(self, flight_machine: FlightStateMachine) -> None:
        mission = build_mission()

        result = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=mission,
            source=EventSource.DISPATCH,
            metadata={"note": "first attempt"},
        )

        flight, event = result.flight, result.audit_event
        assert flight.status is S.PLANNED
        assert (flight.order_id, flight.drone_id, flight.mission) == (ORDER_ID, DRONE_ID, mission)
        assert flight.permission is None
        assert flight.created_at == flight.updated_at == T0
        assert event.event_type is AuditEventType.FLIGHT_CREATED
        assert (event.previous_state, event.new_state) == (None, S.PLANNED)
        assert (event.flight_id, event.order_id, event.drone_id) == (flight.id, ORDER_ID, DRONE_ID)
        assert event.source is EventSource.DISPATCH
        assert event.occurred_at == T0
        assert event.metadata == {
            "note": "first attempt",
            "mission_id": str(mission.mission_id),
            "mission_checksum": mission.checksum,
            "waypoint_count": 5,
            "estimated_duration_s": 600.0,
        }
        assert result.start_authorization is None

    def test_the_machines_own_facts_win_over_caller_metadata(
        self, flight_machine: FlightStateMachine
    ) -> None:
        result = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
            metadata={"mission_checksum": "forged"},
        )
        assert result.audit_event.metadata["mission_checksum"] == build_mission().checksum

    def test_the_flight_and_event_get_different_ids(
        self, flight_machine: FlightStateMachine
    ) -> None:
        result = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        assert result.flight.id != result.audit_event.event_id

    def test_the_default_machine_uses_aware_utc_time_and_random_ids(self) -> None:
        result = FlightStateMachine().create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        assert result.flight.created_at.utcoffset() is not None
        assert isinstance(result.flight.id, UUID)


class TestSuccessfulTransition:
    def test_returns_a_new_flight_and_leaves_the_original_untouched(
        self, flight_machine: FlightStateMachine, clock: FakeClock
    ) -> None:
        original = build_flight(S.PLANNED)
        snapshot = build_flight(S.PLANNED)
        clock.advance(minutes=2)

        result = flight_machine.transition(
            original, S.CANCELLED, source=EventSource.OPS, reason="operator cancelled"
        )

        assert result.flight is not original
        assert original == snapshot
        assert result.flight.updated_at == clock.now
        assert result.flight.created_at == original.created_at

    def test_emits_exactly_one_matching_audit_event(
        self, flight_machine: FlightStateMachine, clock: FakeClock
    ) -> None:
        flight = build_flight(S.MISSION_UPLOADED)
        clock.advance(seconds=5)

        result = flight_machine.transition(
            flight,
            S.CANCELLED,
            source=EventSource.OPS,
            reason="weather",
            metadata={"by": "operator-7"},
        )

        event = result.audit_event
        assert event.event_type is AuditEventType.FLIGHT_STATUS_CHANGED
        assert (event.flight_id, event.order_id, event.drone_id) == (FLIGHT_ID, ORDER_ID, DRONE_ID)
        assert (event.previous_state, event.new_state) == (S.MISSION_UPLOADED, S.CANCELLED)
        assert event.source is EventSource.OPS
        assert event.occurred_at == clock.now == result.flight.updated_at
        assert event.metadata == {"by": "operator-7", "reason": "weather"}

    def test_caller_metadata_is_copied_not_shared(self, flight_machine: FlightStateMachine) -> None:
        metadata: dict[str, Any] = {"nested": {"a": 1}}
        result = flight_machine.transition(
            build_flight(S.PLANNED),
            S.CANCELLED,
            source=EventSource.OPS,
            reason="x",
            metadata=metadata,
        )
        metadata["nested"]["a"] = 2
        assert result.audit_event.metadata["nested"] == {"a": 1}

    def test_the_full_happy_path_leaves_a_consistent_audit_trail(
        self, flight_machine: FlightStateMachine, clock: FakeClock
    ) -> None:
        created = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        flight, events = created.flight, [created.audit_event]
        permission = build_permission(flight_id=flight.id, drone_id=flight.drone_id)

        steps: list[tuple[FlightStatus, dict[str, Any]]] = [
            (S.PERMISSION_GRANTED, {"permission": permission}),
            (S.MISSION_UPLOADED, {"acknowledged_checksum": flight.mission.checksum}),
            (S.STARTED, {}),
            (S.PAUSED, {}),
            (S.STARTED, {}),
            (S.COMPLETED, {}),
        ]
        for target, extra in steps:
            clock.advance(seconds=10)
            if target is S.STARTED:
                assert flight.permission is not None
                extra = {"validation": good_validation(flight, clock.now)}
            result = flight_machine.transition(flight, target, source=EventSource.DISPATCH, **extra)
            flight = result.flight
            events.append(result.audit_event)

        assert flight.status is S.COMPLETED
        assert [e.new_state for e in events] == [
            S.PLANNED,
            S.PERMISSION_GRANTED,
            S.MISSION_UPLOADED,
            S.STARTED,
            S.PAUSED,
            S.STARTED,
            S.COMPLETED,
        ]
        assert [e.previous_state for e in events[1:]] == [e.new_state for e in events[:-1]]
        assert len({e.event_id for e in events}) == len(events)
        assert {e.flight_id for e in events} == {flight.id}
        assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)

    def test_a_flight_status_cannot_be_assigned_directly(self) -> None:
        flight = build_flight(S.MISSION_UPLOADED)
        with pytest.raises(FrozenInstanceError):
            flight.status = S.STARTED  # type: ignore[misc]


class TestTransitionContext:
    @pytest.mark.parametrize("target", list(REQUIRED_CONTEXT), ids=str)
    def test_the_required_context_is_enforced(
        self, flight_machine: FlightStateMachine, ids: SequentialIds, target: FlightStatus
    ) -> None:
        flight = build_flight(SOURCE_FOR[target])
        context = context_for(flight, target)
        del context[REQUIRED_CONTEXT[target]]

        with pytest.raises(MissingTransitionContextError, match=REQUIRED_CONTEXT[target]):
            flight_machine.transition(flight, target, source=EventSource.DISPATCH, **context)
        assert ids.issued == 0

    @pytest.mark.parametrize(
        ("target", "name"),
        [
            (target, name)
            for target in SOURCE_FOR
            for name in CONTEXT_NAMES
            if name != REQUIRED_CONTEXT.get(target)
        ],
        ids=lambda v: str(v),
    )
    def test_context_a_target_does_not_take_is_rejected(
        self,
        flight_machine: FlightStateMachine,
        ids: SequentialIds,
        target: FlightStatus,
        name: str,
    ) -> None:
        flight = build_flight(SOURCE_FOR[target])
        context = context_for(flight, target)
        context[name] = sample_value(name, flight)

        with pytest.raises(FlightValidationError, match=f"'{name}' is not accepted"):
            flight_machine.transition(flight, target, source=EventSource.DISPATCH, **context)
        assert ids.issued == 0

    def test_an_invalid_edge_is_reported_before_context_problems(
        self, flight_machine: FlightStateMachine
    ) -> None:
        with pytest.raises(InvalidFlightTransitionError):
            flight_machine.transition(
                build_flight(S.PLANNED), S.PAUSED, source=EventSource.DISPATCH, reason="x"
            )


class TestPermissionGranted:
    def test_stores_the_permission_and_records_it_in_the_audit_event(
        self, flight_machine: FlightStateMachine
    ) -> None:
        flight = build_flight(S.PLANNED)
        permission = build_permission(permission_id="PERM-77", issuer="authority")

        result = flight_machine.transition(
            flight, S.PERMISSION_GRANTED, source=EventSource.DISPATCH, permission=permission
        )

        assert result.flight.permission == permission
        assert result.audit_event.metadata == {
            "permission_id": "PERM-77",
            "issuer": "authority",
            "valid_from": permission.valid_from.isoformat(),
            "valid_until": permission.valid_until.isoformat(),
        }

    @pytest.mark.parametrize(
        "overrides", [{"flight_id": UUID(int=0x999)}, {"drone_id": UUID(int=0x999)}]
    )
    def test_a_permission_for_another_flight_or_drone_is_rejected(
        self, flight_machine: FlightStateMachine, overrides: dict[str, Any]
    ) -> None:
        with pytest.raises(FlightValidationError, match="different flight or drone"):
            flight_machine.transition(
                build_flight(S.PLANNED),
                S.PERMISSION_GRANTED,
                source=EventSource.DISPATCH,
                permission=build_permission(**overrides),
            )

    @pytest.mark.parametrize("age", [timedelta(minutes=15), timedelta(hours=1)])
    def test_an_already_expired_permission_is_rejected(
        self, flight_machine: FlightStateMachine, clock: FakeClock, age: timedelta
    ) -> None:
        clock.advance(seconds=age.total_seconds())
        with pytest.raises(PermissionExpiredError, match="already expired"):
            flight_machine.transition(
                build_flight(S.PLANNED),
                S.PERMISSION_GRANTED,
                source=EventSource.DISPATCH,
                permission=build_permission(),
            )


class TestMissionUpload:
    def test_records_the_acknowledged_checksum(self, flight_machine: FlightStateMachine) -> None:
        flight = build_flight(S.PERMISSION_GRANTED)

        result = flight_machine.transition(
            flight,
            S.MISSION_UPLOADED,
            source=EventSource.DISPATCH,
            acknowledged_checksum=flight.mission.checksum,
        )

        assert result.flight.acknowledged_checksum == flight.mission.checksum
        assert result.audit_event.metadata == {"mission_checksum": flight.mission.checksum}

    @pytest.mark.parametrize("wrong", ["0" * 64, "", "deadbeef"])
    def test_an_ack_for_a_different_mission_is_rejected(
        self, flight_machine: FlightStateMachine, ids: SequentialIds, wrong: str
    ) -> None:
        flight = build_flight(S.PERMISSION_GRANTED)

        with pytest.raises((MissionChecksumMismatchError, MissingTransitionContextError)):
            flight_machine.transition(
                flight,
                S.MISSION_UPLOADED,
                source=EventSource.DISPATCH,
                acknowledged_checksum=wrong or None,
            )
        assert ids.issued == 0


class TestEndingEarly:
    def test_denial_is_recorded(self, flight_machine: FlightStateMachine) -> None:
        denial = PermissionDenial(DenialCode.NO_FLY_ZONE, "route enters zone 'airport'")

        result = flight_machine.transition(
            build_flight(S.PLANNED), S.PERMISSION_DENIED, source=EventSource.DISPATCH, denial=denial
        )

        assert result.flight.denial == denial
        assert result.audit_event.metadata == {
            "denial_code": "no_fly_zone",
            "denial_reason": "route enters zone 'airport'",
        }

    @pytest.mark.parametrize(
        ("source_status", "target"),
        [
            (S.PERMISSION_GRANTED, S.UPLOAD_FAILED),
            (S.PLANNED, S.CANCELLED),
            (S.MISSION_UPLOADED, S.CANCELLED),
            (S.STARTED, S.ABORTED),
            (S.PAUSED, S.ABORTED),
        ],
    )
    def test_the_reason_is_stored_and_audited(
        self, flight_machine: FlightStateMachine, source_status: FlightStatus, target: FlightStatus
    ) -> None:
        flight = build_flight(source_status)

        result = flight_machine.transition(
            flight, target, source=EventSource.DISPATCH, reason="return_to_home"
        )

        assert result.flight.failure_reason == "return_to_home"
        assert result.audit_event.metadata == {"reason": "return_to_home"}

    @pytest.mark.parametrize("target", [S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED])
    def test_a_blank_reason_is_rejected(
        self, flight_machine: FlightStateMachine, target: FlightStatus
    ) -> None:
        with pytest.raises(FlightValidationError, match="blank"):
            flight_machine.transition(
                build_flight(SOURCE_FOR[target]), target, source=EventSource.OPS, reason="  "
            )


class TestStartGate:
    """Rule 2: a flight starts (or resumes) only with a valid, fresh, sufficient permission."""

    @pytest.fixture(params=[S.MISSION_UPLOADED, S.PAUSED], ids=["first start", "resume"])
    def flight(self, request: pytest.FixtureRequest) -> Flight:
        return build_flight(request.param)

    def start(
        self, machine: FlightStateMachine, flight: Flight, **validation_overrides: Any
    ) -> Any:
        return machine.transition(
            flight,
            S.STARTED,
            source=EventSource.DISPATCH,
            validation=good_validation(flight, machine._clock(), **validation_overrides),
        )

    # --- it passes --------------------------------------------------------------------------

    def test_a_valid_fresh_sufficient_permission_starts_the_flight(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        result = self.start(flight_machine, flight)

        assert result.flight.status is S.STARTED
        assert result.audit_event.new_state is S.STARTED
        assert result.start_authorization is not None

    def test_the_start_authorisation_is_bound_to_this_flight_permission_and_mission(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        assert flight.permission is not None

        authorization = self.start(flight_machine, flight).start_authorization

        assert authorization == StartAuthorization(
            flight_id=flight.id,
            drone_id=flight.drone_id,
            permission_id=flight.permission.permission_id,
            mission_checksum=flight.mission.checksum,
            authorized_at=T0,
            valid_until=flight.permission.valid_until,
        )

    def test_the_audit_event_records_what_the_gate_relied_on(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        metadata = self.start(flight_machine, flight).audit_event.metadata

        assert metadata["permission_id"] == "PERM-1"
        assert metadata["validated_at"] == T0.isoformat()
        assert metadata["permission_valid_until"] == (T0 + timedelta(minutes=15)).isoformat()
        assert metadata["resumed"] is (flight.status is S.PAUSED)

    def test_started_at_is_set_on_the_first_start_and_kept_on_resume(
        self, flight_machine: FlightStateMachine, clock: FakeClock
    ) -> None:
        first = build_flight(S.MISSION_UPLOADED)
        clock.advance(seconds=20)
        started = self.start(flight_machine, first).flight
        assert started.started_at == clock.now

        earlier = T0 - timedelta(hours=1)
        paused = build_flight(S.PAUSED, started_at=earlier)
        assert self.start(flight_machine, paused).flight.started_at == earlier

    # --- it is refused ----------------------------------------------------------------------

    def test_no_validation_means_no_start(
        self, flight_machine: FlightStateMachine, ids: SequentialIds, flight: Flight
    ) -> None:
        with pytest.raises(MissingTransitionContextError, match="validation"):
            flight_machine.transition(flight, S.STARTED, source=EventSource.DISPATCH)
        assert ids.issued == 0

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (PermissionStatus.REVOKED, PermissionRevokedError),
            (PermissionStatus.UNKNOWN, PermissionInvalidError),
            (PermissionStatus.EXPIRED, PermissionExpiredError),
        ],
    )
    def test_the_provider_saying_anything_but_valid_blocks_the_start(
        self,
        flight_machine: FlightStateMachine,
        ids: SequentialIds,
        flight: Flight,
        status: PermissionStatus,
        error: type[FlightPermissionError],
    ) -> None:
        with pytest.raises(error):
            self.start(flight_machine, flight, status=status, valid_until=None)
        assert ids.issued == 0

    def test_a_check_about_a_different_permission_is_rejected(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        with pytest.raises(PermissionInvalidError, match="different permission"):
            self.start(flight_machine, flight, permission_id="SOMEONE-ELSES")

    def test_a_flight_that_failed_the_gate_is_unchanged(
        self, flight_machine: FlightStateMachine, ids: SequentialIds, flight: Flight
    ) -> None:
        before = flight
        with pytest.raises(PermissionRevokedError):
            self.start(flight_machine, flight, status=PermissionStatus.REVOKED, valid_until=None)
        assert flight == before
        assert ids.issued == 0

    # --- freshness of the compliance check --------------------------------------------------

    @pytest.mark.parametrize("age_seconds", [31, 60, 3600])
    def test_a_stale_check_is_rejected(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight, age_seconds: int
    ) -> None:
        stale = good_validation(flight, T0)
        clock.advance(seconds=age_seconds)

        with pytest.raises(PermissionCheckStaleError, match="old"):
            flight_machine.transition(
                flight, S.STARTED, source=EventSource.DISPATCH, validation=stale
            )

    def test_a_check_exactly_at_the_age_limit_is_accepted(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight
    ) -> None:
        assert timedelta(seconds=30) == DEFAULT_MAX_VALIDATION_AGE
        check = good_validation(flight, T0)
        clock.advance(seconds=30)

        result = flight_machine.transition(
            flight, S.STARTED, source=EventSource.DISPATCH, validation=check
        )

        assert result.flight.status is S.STARTED

    def test_the_age_limit_is_configurable(
        self, clock: FakeClock, ids: SequentialIds, flight: Flight
    ) -> None:
        strict = FlightStateMachine(
            clock=clock, id_factory=ids, max_validation_age=timedelta(seconds=5)
        )
        check = good_validation(flight, T0)
        clock.advance(seconds=6)

        with pytest.raises(PermissionCheckStaleError):
            strict.transition(flight, S.STARTED, source=EventSource.DISPATCH, validation=check)

    def test_a_check_dated_in_the_future_is_rejected(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        future = good_validation(flight, T0 + timedelta(minutes=1))

        with pytest.raises(PermissionCheckStaleError, match="future"):
            flight_machine.transition(
                flight, S.STARTED, source=EventSource.DISPATCH, validation=future
            )

    # --- the permission's validity window ---------------------------------------------------

    def test_a_permission_that_has_ended_is_rejected_even_if_the_check_is_fresh(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight
    ) -> None:
        clock.advance(minutes=15)  # now == valid_until: the permission has just ended

        with pytest.raises(PermissionExpiredError):
            self.start(flight_machine, flight)

    def test_a_permission_that_is_not_valid_yet_is_rejected(
        self, flight_machine: FlightStateMachine
    ) -> None:
        later = build_permission(
            valid_from=T0 + timedelta(hours=1), valid_until=T0 + timedelta(hours=2)
        )
        flight = build_flight(S.MISSION_UPLOADED, permission=later)

        with pytest.raises(PermissionInvalidError, match="not valid yet"):
            self.start(flight_machine, flight)

    def test_the_provider_reporting_an_earlier_end_than_we_stored_wins(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        # We stored 15 minutes, the provider now says 5; the mission needs 10.
        with pytest.raises(PermissionWindowTooShortError):
            self.start(flight_machine, flight, valid_until=T0 + timedelta(minutes=5))

    def test_the_provider_reporting_an_end_in_the_past_is_expiry(
        self, flight_machine: FlightStateMachine, flight: Flight
    ) -> None:
        with pytest.raises(PermissionExpiredError):
            self.start(flight_machine, flight, valid_until=T0 - timedelta(seconds=1))

    def test_the_provider_reporting_a_later_end_does_not_extend_our_stored_permission(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight
    ) -> None:
        clock.advance(minutes=15)  # past what we stored

        with pytest.raises(PermissionExpiredError):
            self.start(flight_machine, flight, valid_until=T0 + timedelta(days=1))

    # --- the permission must cover the whole mission ----------------------------------------

    def test_a_permission_ending_exactly_when_the_mission_would_finish_is_enough(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight
    ) -> None:
        clock.advance(seconds=300)  # 600s of permission left = the mission's 600s

        assert self.start(flight_machine, flight).flight.status is S.STARTED

    def test_one_second_short_is_not(
        self, flight_machine: FlightStateMachine, clock: FakeClock, flight: Flight
    ) -> None:
        clock.advance(seconds=301)

        with pytest.raises(PermissionWindowTooShortError, match="before the mission would finish"):
            self.start(flight_machine, flight)

    def test_a_long_mission_needs_a_long_permission(
        self, flight_machine: FlightStateMachine
    ) -> None:
        long_mission = build_mission(estimated_duration_s=3600.0)
        flight = build_flight(S.MISSION_UPLOADED, mission=long_mission)

        with pytest.raises(PermissionWindowTooShortError):
            self.start(flight_machine, flight)

    # --- defence in depth -------------------------------------------------------------------

    def test_a_flight_somehow_missing_its_permission_cannot_start(
        self, flight_machine: FlightStateMachine
    ) -> None:
        """Unreachable through the public API (the Flight invariants forbid it), but the gate
        must not depend on that: corrupt the object and it still refuses."""
        flight = build_flight(S.MISSION_UPLOADED)
        valid = good_validation(flight)
        object.__setattr__(flight, "permission", None)

        with pytest.raises(PermissionNotGrantedError):
            flight_machine.transition(
                flight, S.STARTED, source=EventSource.DISPATCH, validation=valid
            )


class TestSafetyCommandsAreNeverGated:
    """Pause, abort (return-to-home / land) and complete must work even with no permission."""

    @pytest.fixture
    def long_after_expiry(self, clock: FakeClock) -> FakeClock:
        clock.advance(days=1)
        return clock

    @pytest.mark.parametrize("status", [S.STARTED, S.PAUSED])
    def test_aborting_works_when_the_permission_has_long_expired(
        self, flight_machine: FlightStateMachine, long_after_expiry: FakeClock, status: FlightStatus
    ) -> None:
        flight = build_flight(status)

        result = flight_machine.transition(
            flight, S.ABORTED, source=EventSource.OPS, reason="return_to_home"
        )

        assert result.flight.status is S.ABORTED
        assert result.start_authorization is None

    def test_pausing_works_when_the_permission_has_long_expired(
        self, flight_machine: FlightStateMachine, long_after_expiry: FakeClock
    ) -> None:
        result = flight_machine.transition(
            build_flight(S.STARTED), S.PAUSED, source=EventSource.OPS
        )
        assert result.flight.status is S.PAUSED

    def test_completing_works_when_the_permission_has_long_expired(
        self, flight_machine: FlightStateMachine, long_after_expiry: FlightStateMachine
    ) -> None:
        result = flight_machine.transition(
            build_flight(S.STARTED), S.COMPLETED, source=EventSource.TELEMETRY
        )
        assert result.flight.status is S.COMPLETED

    @pytest.mark.parametrize("target", [S.PAUSED, S.COMPLETED, S.ABORTED])
    def test_a_permission_check_cannot_be_smuggled_into_a_safety_transition(
        self, flight_machine: FlightStateMachine, target: FlightStatus
    ) -> None:
        flight = build_flight(S.STARTED)
        extra = {"reason": "x"} if target is S.ABORTED else {}

        with pytest.raises(FlightValidationError, match="'validation' is not accepted"):
            flight_machine.transition(
                flight,
                target,
                source=EventSource.OPS,
                validation=good_validation(flight),
                **extra,
            )

    def test_safety_transitions_never_produce_a_start_authorisation(
        self, flight_machine: FlightStateMachine
    ) -> None:
        for target in (S.PAUSED, S.COMPLETED):
            result = flight_machine.transition(
                build_flight(S.STARTED), target, source=EventSource.OPS
            )
            assert result.start_authorization is None


class TestStartAuthorization:
    def test_must_be_valid_after_it_is_issued(self) -> None:
        with pytest.raises(FlightValidationError, match="valid after"):
            StartAuthorization(FLIGHT_ID, DRONE_ID, "P", "c" * 64, T0, T0)

    def test_is_immutable(self) -> None:
        authorization = StartAuthorization(
            FLIGHT_ID, DRONE_ID, "P", "c" * 64, T0, T0 + timedelta(minutes=1)
        )
        with pytest.raises(FrozenInstanceError):
            authorization.permission_id = "other"  # type: ignore[misc]
