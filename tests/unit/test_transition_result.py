"""TransitionResult refuses to exist unless it is consistent (the repository trusts it)."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest

from app.domain.exceptions import InconsistentTransitionResultError, InvalidTransitionError
from app.domain.models import (
    AuditEvent,
    AuditEventType,
    EventSource,
    Location,
    Order,
    OrderStatus,
    Package,
)
from app.domain.order_state_machine import OrderStateMachine, TransitionResult
from tests.conftest import DRONE_ID, T0

S = OrderStatus
MakeOrder = Callable[..., Order]


def changed_event(order: Order, previous: OrderStatus, *, drone_id: UUID | None) -> AuditEvent:
    return AuditEvent(
        event_id=UUID(int=1),
        order_id=order.id,
        event_type=AuditEventType.ORDER_STATUS_CHANGED,
        previous_state=previous,
        new_state=order.status,
        occurred_at=T0,
        source=EventSource.API,
        drone_id=drone_id,
    )


class TestResultsFromTheStateMachineAreAccepted:
    def test_create(
        self, machine: OrderStateMachine, pickup: Location, dropoff: Location, package: Package
    ) -> None:
        result = machine.create(
            client_id="c",
            external_order_id="e",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=EventSource.API,
        )
        assert result.order.status is S.CREATED

    def test_transition(self, machine: OrderStateMachine, make_order: MakeOrder) -> None:
        result = machine.transition(
            make_order(S.IN_FLIGHT), S.ARRIVING, source=EventSource.TELEMETRY
        )
        assert result.order.status is S.ARRIVING


class TestInconsistentResultsAreRejected:
    def test_an_event_for_a_different_order(self, make_order: MakeOrder) -> None:
        order = make_order(S.CANCELLED, drone_id=None)
        event = changed_event(
            make_order(S.CANCELLED, drone_id=None, id=UUID(int=999)), S.CREATED, drone_id=None
        )

        with pytest.raises(InconsistentTransitionResultError):
            TransitionResult(order=order, audit_event=event)

    def test_an_event_whose_new_state_is_not_the_orders_status(self, make_order: MakeOrder) -> None:
        order = make_order(S.CANCELLED, drone_id=None)
        event = changed_event(make_order(S.FAILED, drone_id=None), S.CREATED, drone_id=None)

        with pytest.raises(InconsistentTransitionResultError):
            TransitionResult(order=order, audit_event=event)

    def test_an_event_with_a_different_drone(self, make_order: MakeOrder) -> None:
        order = make_order(S.PICKED_UP)
        event = changed_event(order, S.ASSIGNED, drone_id=UUID(int=0xBAD))

        with pytest.raises(InconsistentTransitionResultError):
            TransitionResult(order=order, audit_event=event)

    @pytest.mark.parametrize(
        ("previous", "new"),
        [
            (S.CREATED, S.DELIVERED),
            (S.CREATED, S.IN_FLIGHT),
            (S.ASSIGNED, S.ARRIVING),
            (S.DELIVERED, S.FAILED),
            (S.CANCELLED, S.CREATED),
            (S.PICKED_UP, S.RETURNED),
        ],
    )
    def test_a_fabricated_edge_cannot_be_wrapped_in_a_result(
        self, make_order: MakeOrder, previous: OrderStatus, new: OrderStatus
    ) -> None:
        """Even with a perfectly matching order and event, an illegal edge is refused."""
        drone = DRONE_ID if new not in {S.CREATED, S.FAILED, S.CANCELLED} else None
        order = make_order(new, drone_id=drone)
        event = changed_event(order, previous, drone_id=drone)

        with pytest.raises(InvalidTransitionError):
            TransitionResult(order=order, audit_event=event)

    def test_a_legal_hand_built_edge_is_accepted(self, make_order: MakeOrder) -> None:
        """The check is about legality, so a correctly matched legal edge constructs fine."""
        order = make_order(S.PICKED_UP)
        result = TransitionResult(
            order=order, audit_event=changed_event(order, S.ASSIGNED, drone_id=DRONE_ID)
        )
        assert result.order is order
