"""FlightTransitionResult refuses to exist inconsistently (the repository trusts it)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import InconsistentTransitionResultError, InvalidFlightTransitionError
from app.domain.flight_state_machine import (
    FlightStateMachine,
    FlightTransitionResult,
    StartAuthorization,
)
from app.domain.models import (
    AuditEvent,
    AuditEventType,
    EventSource,
    Flight,
    FlightStatus,
    OrderStatus,
    PermissionStatus,
    PermissionValidation,
)
from tests.conftest import DRONE_ID, FLIGHT_ID, ORDER_ID, T0, build_flight, build_mission

S = FlightStatus


def event_for(flight: Flight, previous: FlightStatus | None, **overrides: Any) -> AuditEvent:
    fields: dict[str, Any] = {
        "event_id": UUID(int=0xE1),
        "order_id": flight.order_id,
        "event_type": (
            AuditEventType.FLIGHT_CREATED
            if previous is None
            else AuditEventType.FLIGHT_STATUS_CHANGED
        ),
        "previous_state": previous,
        "new_state": flight.status,
        "occurred_at": T0,
        "source": EventSource.DISPATCH,
        "drone_id": flight.drone_id,
        "flight_id": flight.id,
    }
    fields.update(overrides)
    return AuditEvent(**fields)


def a_real_start(machine: FlightStateMachine) -> FlightTransitionResult:
    flight = build_flight(S.MISSION_UPLOADED)
    assert flight.permission is not None
    return machine.transition(
        flight,
        S.STARTED,
        source=EventSource.DISPATCH,
        validation=PermissionValidation(
            flight.permission.permission_id,
            PermissionStatus.VALID,
            T0,
            flight.permission.valid_until,
        ),
    )


class TestResultsFromTheStateMachineAreAccepted:
    def test_create(self, flight_machine: FlightStateMachine) -> None:
        result = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        assert result.flight.status is S.PLANNED

    def test_a_start_comes_with_its_authorisation(self, flight_machine: FlightStateMachine) -> None:
        assert a_real_start(flight_machine).start_authorization is not None

    def test_any_other_transition(self, flight_machine: FlightStateMachine) -> None:
        result = flight_machine.transition(
            build_flight(S.STARTED), S.PAUSED, source=EventSource.OPS
        )
        assert result.start_authorization is None


class TestTheEventMustDescribeTheFlight:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"flight_id": UUID(int=0x999)},
            {"order_id": UUID(int=0x999)},
            {"drone_id": UUID(int=0x999)},
        ],
    )
    def test_a_different_flight_order_or_drone(self, overrides: dict[str, Any]) -> None:
        flight = build_flight(S.PAUSED)

        with pytest.raises(InconsistentTransitionResultError, match="does not describe"):
            FlightTransitionResult(flight, event_for(flight, S.STARTED, **overrides))

    def test_a_new_state_that_is_not_the_flights_status(self) -> None:
        flight = build_flight(S.PAUSED)

        with pytest.raises(InconsistentTransitionResultError):
            FlightTransitionResult(flight, event_for(flight, S.STARTED, new_state=S.COMPLETED))

    def test_an_order_event_is_not_a_flight_event(self) -> None:
        flight = build_flight(S.PLANNED)
        order_event = AuditEvent(
            event_id=UUID(int=1),
            order_id=ORDER_ID,
            event_type=AuditEventType.ORDER_CREATED,
            previous_state=None,
            new_state=OrderStatus.CREATED,
            occurred_at=T0,
            source=EventSource.API,
        )

        with pytest.raises(InconsistentTransitionResultError, match="flight audit event"):
            FlightTransitionResult(flight, order_event)


class TestAFabricatedEdgeCannotBeWrapped:
    @pytest.mark.parametrize(
        ("previous", "new"),
        [
            (S.PLANNED, S.STARTED),
            (S.PLANNED, S.COMPLETED),
            (S.PERMISSION_GRANTED, S.STARTED),
            (S.COMPLETED, S.STARTED),
            (S.CANCELLED, S.PLANNED),
            (S.ABORTED, S.PAUSED),
            (S.PAUSED, S.COMPLETED),
        ],
    )
    def test_even_with_a_perfectly_matching_flight_and_event(
        self, previous: FlightStatus, new: FlightStatus
    ) -> None:
        flight = build_flight(new)

        with pytest.raises(InvalidFlightTransitionError):
            FlightTransitionResult(flight, event_for(flight, previous))


class TestTheStartAuthorisation:
    def test_entering_started_without_one_is_refused(
        self, flight_machine: FlightStateMachine
    ) -> None:
        real = a_real_start(flight_machine)

        with pytest.raises(InconsistentTransitionResultError, match="start authorisation"):
            FlightTransitionResult(real.flight, real.audit_event, start_authorization=None)

    def test_an_authorisation_on_any_other_transition_is_refused(
        self, flight_machine: FlightStateMachine
    ) -> None:
        real = a_real_start(flight_machine)
        paused = flight_machine.transition(real.flight, S.PAUSED, source=EventSource.OPS)

        with pytest.raises(InconsistentTransitionResultError, match="start authorisation"):
            FlightTransitionResult(
                paused.flight, paused.audit_event, start_authorization=real.start_authorization
            )

    def test_an_authorisation_on_creation_is_refused(
        self, flight_machine: FlightStateMachine
    ) -> None:
        real = a_real_start(flight_machine)
        created = flight_machine.create(
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )

        with pytest.raises(InconsistentTransitionResultError):
            FlightTransitionResult(
                created.flight, created.audit_event, start_authorization=real.start_authorization
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"flight_id": UUID(int=0x999)},
            {"drone_id": UUID(int=0x999)},
            {"permission_id": "SOMEONE-ELSES"},
            {"mission_checksum": "0" * 64},
        ],
    )
    def test_an_authorisation_for_another_flight_permission_or_mission_is_refused(
        self, flight_machine: FlightStateMachine, overrides: dict[str, Any]
    ) -> None:
        real = a_real_start(flight_machine)
        assert real.start_authorization is not None
        forged = replace(real.start_authorization, **overrides)

        with pytest.raises(InconsistentTransitionResultError, match="does not match"):
            FlightTransitionResult(real.flight, real.audit_event, start_authorization=forged)

    def test_a_hand_made_authorisation_with_matching_fields_still_needs_a_real_started_flight(
        self,
    ) -> None:
        """The token is only meaningful next to a started flight and its audit event."""
        flight = build_flight(S.PAUSED)
        token = StartAuthorization(
            FLIGHT_ID,
            DRONE_ID,
            "PERM-1",
            flight.mission.checksum,
            T0,
            T0 + timedelta(minutes=5),
        )

        with pytest.raises(InconsistentTransitionResultError):
            FlightTransitionResult(flight, event_for(flight, S.STARTED), start_authorization=token)
