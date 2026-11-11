"""Order state machine: transition rules, immutability and audit output (Rules 4 and 5)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.domain.exceptions import (
    DomainError,
    InvalidTransitionError,
    MissingTransitionContextError,
    OrderValidationError,
    TerminalStateError,
)
from app.domain.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AuditEventType,
    EventSource,
    Location,
    Order,
    OrderStatus,
    Package,
)
from app.domain.order_state_machine import ALLOWED_TRANSITIONS, OrderStateMachine
from tests.conftest import DRONE_ID, T0, FakeClock, SequentialIds

S = OrderStatus

# The spec, written out by hand and deliberately NOT derived from the
# implementation, so the test can catch a wrong table.
ALLOWED_EDGES: frozenset[tuple[OrderStatus, OrderStatus]] = frozenset(
    {
        # normal lifecycle
        (S.CREATED, S.ASSIGNED),
        (S.ASSIGNED, S.PICKED_UP),
        (S.PICKED_UP, S.IN_FLIGHT),
        (S.IN_FLIGHT, S.ARRIVING),
        (S.ARRIVING, S.DELIVERED),
        # any active state -> failed
        (S.CREATED, S.FAILED),
        (S.ASSIGNED, S.FAILED),
        (S.PICKED_UP, S.FAILED),
        (S.IN_FLIGHT, S.FAILED),
        (S.ARRIVING, S.FAILED),
        # any active state -> cancelled
        (S.CREATED, S.CANCELLED),
        (S.ASSIGNED, S.CANCELLED),
        (S.PICKED_UP, S.CANCELLED),
        (S.IN_FLIGHT, S.CANCELLED),
        (S.ARRIVING, S.CANCELLED),
        # return flow
        (S.IN_FLIGHT, S.RETURNED),
        (S.ARRIVING, S.RETURNED),
    }
)
ALL_PAIRS = list(itertools.product(OrderStatus, OrderStatus))
PAIR_IDS = [f"{src}->{dst}" for src, dst in ALL_PAIRS]
MakeOrder = Callable[..., Order]


def _drone_kwargs(target: OrderStatus) -> dict[str, UUID]:
    return {"drone_id": DRONE_ID} if target is S.ASSIGNED else {}


class TestTransitionTable:
    def test_spec_has_17_edges(self) -> None:
        assert len(ALLOWED_EDGES) == 17

    def test_table_matches_spec_exactly(self) -> None:
        actual = {(src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in targets}
        assert actual == ALLOWED_EDGES

    def test_every_status_has_a_row(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)

    def test_active_and_terminal_states_partition_all_statuses(self) -> None:
        assert set(OrderStatus) == ACTIVE_STATES | TERMINAL_STATES
        assert not ACTIVE_STATES & TERMINAL_STATES
        assert {
            S.DELIVERED,
            S.FAILED,
            S.CANCELLED,
            S.RETURNED,
        } == TERMINAL_STATES

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        for status in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_every_active_state_can_fail_or_be_cancelled(self) -> None:
        for status in ACTIVE_STATES:
            assert {S.FAILED, S.CANCELLED} <= ALLOWED_TRANSITIONS[status]

    def test_table_cannot_be_modified(self) -> None:
        with pytest.raises(TypeError):
            ALLOWED_TRANSITIONS[S.DELIVERED] = frozenset({S.CREATED})  # type: ignore[index]
        with pytest.raises(AttributeError):
            ALLOWED_TRANSITIONS[S.CREATED].add(S.DELIVERED)  # type: ignore[attr-defined]

    @pytest.mark.parametrize(("src", "dst"), ALL_PAIRS, ids=PAIR_IDS)
    def test_can_transition_agrees_with_spec(
        self, machine: OrderStateMachine, src: OrderStatus, dst: OrderStatus
    ) -> None:
        assert machine.can_transition(src, dst) is ((src, dst) in ALLOWED_EDGES)

    def test_allowed_targets(self, machine: OrderStateMachine) -> None:
        assert machine.allowed_targets(S.IN_FLIGHT) == {
            S.ARRIVING,
            S.FAILED,
            S.CANCELLED,
            S.RETURNED,
        }


class TestTransitionMatrix:
    """Every one of the 81 (from, to) pairs behaves exactly as the spec says."""

    @pytest.mark.parametrize(("src", "dst"), ALL_PAIRS, ids=PAIR_IDS)
    def test_pair(
        self,
        machine: OrderStateMachine,
        make_order: MakeOrder,
        ids: SequentialIds,
        src: OrderStatus,
        dst: OrderStatus,
    ) -> None:
        order = make_order(src)

        if (src, dst) in ALLOWED_EDGES:
            result = machine.transition(
                order, dst, source=EventSource.DISPATCH, **_drone_kwargs(dst)
            )
            assert result.order.status is dst
            assert result.audit_event.previous_state is src
            assert result.audit_event.new_state is dst
            return

        expected = TerminalStateError if src in TERMINAL_STATES else InvalidTransitionError
        issued_before = ids.issued
        with pytest.raises(InvalidTransitionError) as exc_info:
            machine.transition(order, dst, source=EventSource.DISPATCH, **_drone_kwargs(dst))
        assert type(exc_info.value) is expected
        assert exc_info.value.current is src
        assert exc_info.value.target is dst
        assert ids.issued == issued_before, "a rejected transition must not mint an audit event"
        assert order.status is src


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            (S.CREATED, S.IN_FLIGHT),
            (S.CREATED, S.DELIVERED),
            (S.ASSIGNED, S.DELIVERED),
            (S.PICKED_UP, S.ARRIVING),
            (S.IN_FLIGHT, S.DELIVERED),
            (S.ARRIVING, S.PICKED_UP),
            (S.IN_FLIGHT, S.ASSIGNED),
            (S.CREATED, S.RETURNED),
            (S.ASSIGNED, S.RETURNED),
            (S.PICKED_UP, S.RETURNED),
        ],
    )
    def test_skipped_and_backwards_steps_are_rejected(
        self, machine: OrderStateMachine, make_order: MakeOrder, src: OrderStatus, dst: OrderStatus
    ) -> None:
        with pytest.raises(InvalidTransitionError):
            machine.transition(make_order(src), dst, source=EventSource.API, **_drone_kwargs(dst))

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_moving_to_the_current_status_is_rejected(
        self, machine: OrderStateMachine, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        with pytest.raises(InvalidTransitionError):
            machine.transition(make_order(status), status, source=EventSource.API)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
    def test_terminal_states_raise_terminal_state_error(
        self, machine: OrderStateMachine, make_order: MakeOrder, terminal: OrderStatus
    ) -> None:
        for target in OrderStatus:
            with pytest.raises(TerminalStateError):
                machine.transition(make_order(terminal), target, source=EventSource.API)

    def test_a_delivered_order_cannot_be_failed_or_cancelled(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        for target in (S.FAILED, S.CANCELLED):
            with pytest.raises(TerminalStateError):
                machine.transition(make_order(S.DELIVERED), target, source=EventSource.API)

    def test_an_invalid_edge_is_reported_before_context_problems(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        with pytest.raises(InvalidTransitionError):
            machine.transition(
                make_order(S.IN_FLIGHT), S.PICKED_UP, source=EventSource.API, drone_id=DRONE_ID
            )

    def test_exception_hierarchy(self) -> None:
        assert issubclass(TerminalStateError, InvalidTransitionError)
        assert issubclass(InvalidTransitionError, DomainError)
        assert issubclass(MissingTransitionContextError, DomainError)
        assert issubclass(OrderValidationError, DomainError)

    def test_error_message_names_both_states(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        with pytest.raises(InvalidTransitionError, match=r"created -> delivered"):
            machine.transition(make_order(S.CREATED), S.DELIVERED, source=EventSource.API)


class TestCreate:
    def test_creates_order_in_created_state(
        self, machine: OrderStateMachine, pickup: Location, dropoff: Location, package: Package
    ) -> None:
        result = machine.create(
            client_id="mock",
            external_order_id="ORD-7",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=EventSource.ADAPTER,
        )
        order = result.order
        assert order.status is S.CREATED
        assert order.drone_id is None
        assert order.client_id == "mock"
        assert order.external_order_id == "ORD-7"
        assert (order.pickup, order.dropoff, order.package) == (pickup, dropoff, package)
        assert order.created_at == order.updated_at == T0

    def test_emits_an_order_created_audit_event(
        self, machine: OrderStateMachine, pickup: Location, dropoff: Location, package: Package
    ) -> None:
        result = machine.create(
            client_id="mock",
            external_order_id="ORD-7",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=EventSource.ADAPTER,
            metadata={"client": "mock"},
        )
        event = result.audit_event
        assert event.event_type is AuditEventType.ORDER_CREATED
        assert event.order_id == result.order.id
        assert event.previous_state is None
        assert event.new_state is S.CREATED
        assert event.drone_id is None
        assert event.source is EventSource.ADAPTER
        assert event.occurred_at == T0
        assert event.metadata == {"client": "mock"}
        assert event.event_id != result.order.id

    def test_invalid_input_raises_and_mints_nothing(
        self, machine: OrderStateMachine, pickup: Location, package: Package, ids: SequentialIds
    ) -> None:
        with pytest.raises(OrderValidationError):
            machine.create(
                client_id="mock",
                external_order_id="ORD-7",
                pickup=pickup,
                dropoff=pickup,
                package=package,
                source=EventSource.API,
            )
        # The order id is drawn first; no audit-event id may follow it.
        assert ids.issued <= 1


class TestSuccessfulTransition:
    def test_returns_new_order_and_leaves_the_original_untouched(
        self, machine: OrderStateMachine, make_order: MakeOrder, clock: FakeClock
    ) -> None:
        original = make_order(S.PICKED_UP)
        snapshot = make_order(S.PICKED_UP)
        clock.advance(minutes=5)

        result = machine.transition(original, S.IN_FLIGHT, source=EventSource.TELEMETRY)

        assert result.order is not original
        assert result.order.status is S.IN_FLIGHT
        assert original == snapshot
        assert original.status is S.PICKED_UP
        assert result.order.updated_at == clock.now
        assert result.order.created_at == original.created_at

    def test_other_fields_are_carried_over(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        original = make_order(S.ASSIGNED)
        updated = machine.transition(original, S.PICKED_UP, source=EventSource.DISPATCH).order
        for name in ("id", "client_id", "external_order_id", "pickup", "dropoff", "package"):
            assert getattr(updated, name) == getattr(original, name)

    def test_emits_exactly_one_matching_audit_event(
        self, machine: OrderStateMachine, make_order: MakeOrder, clock: FakeClock
    ) -> None:
        order = make_order(S.IN_FLIGHT)
        clock.advance(seconds=90)

        result = machine.transition(
            order,
            S.ARRIVING,
            source=EventSource.TELEMETRY,
            metadata={"distance_to_dropoff_m": 120, "tags": ["geofence", "entered"]},
        )

        event = result.audit_event
        assert event.event_type is AuditEventType.ORDER_STATUS_CHANGED
        assert event.order_id == order.id
        assert event.drone_id == DRONE_ID
        assert event.previous_state is S.IN_FLIGHT
        assert event.new_state is S.ARRIVING
        assert event.source is EventSource.TELEMETRY
        assert event.occurred_at == clock.now == result.order.updated_at
        assert event.metadata == {"distance_to_dropoff_m": 120, "tags": ("geofence", "entered")}

    def test_every_event_gets_a_unique_id(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        order = make_order(S.CREATED)
        event_ids = set()
        for target, extra in [
            (S.ASSIGNED, {"drone_id": DRONE_ID}),
            (S.PICKED_UP, {}),
            (S.IN_FLIGHT, {}),
        ]:
            result = machine.transition(order, target, source=EventSource.DISPATCH, **extra)
            event_ids.add(result.audit_event.event_id)
            order = result.order
        assert len(event_ids) == 3

    def test_caller_metadata_is_copied_not_shared(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        metadata: dict[str, object] = {"reason": "customer request", "nested": {"a": 1}}
        result = machine.transition(
            make_order(S.CREATED), S.CANCELLED, source=EventSource.API, metadata=metadata
        )
        metadata["reason"] = "tampered"
        metadata["nested"]["a"] = 2  # type: ignore[index]
        assert result.audit_event.metadata == {"reason": "customer request", "nested": {"a": 1}}

    def test_transitions_without_metadata_get_an_empty_mapping(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        result = machine.transition(make_order(S.CREATED), S.CANCELLED, source=EventSource.OPS)
        assert result.audit_event.metadata == {}

    def test_full_happy_path_produces_a_consistent_audit_trail(
        self,
        machine: OrderStateMachine,
        clock: FakeClock,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        created = machine.create(
            client_id="mock",
            external_order_id="ORD-1",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=EventSource.API,
        )
        order, events = created.order, [created.audit_event]

        steps: list[tuple[OrderStatus, dict[str, UUID]]] = [
            (S.ASSIGNED, {"drone_id": DRONE_ID}),
            (S.PICKED_UP, {}),
            (S.IN_FLIGHT, {}),
            (S.ARRIVING, {}),
            (S.DELIVERED, {}),
        ]
        for target, extra in steps:
            clock.advance(minutes=1)
            result = machine.transition(order, target, source=EventSource.DISPATCH, **extra)
            order = result.order
            events.append(result.audit_event)

        assert order.status is S.DELIVERED
        assert order.drone_id == DRONE_ID
        assert [e.new_state for e in events] == [
            S.CREATED,
            S.ASSIGNED,
            S.PICKED_UP,
            S.IN_FLIGHT,
            S.ARRIVING,
            S.DELIVERED,
        ]
        # Each event starts where the previous one ended.
        assert [e.previous_state for e in events[1:]] == [e.new_state for e in events[:-1]]
        assert len({e.event_id for e in events}) == len(events)
        assert {e.order_id for e in events} == {order.id}
        assert [e.occurred_at for e in events] == sorted(e.occurred_at for e in events)

    @pytest.mark.parametrize("status", sorted(ACTIVE_STATES))
    @pytest.mark.parametrize("target", [S.FAILED, S.CANCELLED])
    def test_failure_and_cancellation_keep_the_drone_if_there_is_one(
        self,
        machine: OrderStateMachine,
        make_order: MakeOrder,
        status: OrderStatus,
        target: OrderStatus,
    ) -> None:
        order = make_order(status)
        result = machine.transition(order, target, source=EventSource.OPS)
        assert result.order.drone_id == order.drone_id
        assert result.audit_event.drone_id == order.drone_id

    @pytest.mark.parametrize("status", [S.IN_FLIGHT, S.ARRIVING])
    def test_return_flow(
        self, machine: OrderStateMachine, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        result = machine.transition(make_order(status), S.RETURNED, source=EventSource.DISPATCH)
        assert result.order.status is S.RETURNED
        assert result.order.status.is_terminal


class TestDroneContext:
    def test_assigning_requires_a_drone(
        self, machine: OrderStateMachine, make_order: MakeOrder, ids: SequentialIds
    ) -> None:
        with pytest.raises(MissingTransitionContextError):
            machine.transition(make_order(S.CREATED), S.ASSIGNED, source=EventSource.DISPATCH)
        assert ids.issued == 0

    def test_assigning_records_the_drone_on_order_and_event(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        result = machine.transition(
            make_order(S.CREATED), S.ASSIGNED, source=EventSource.DISPATCH, drone_id=DRONE_ID
        )
        assert result.order.drone_id == DRONE_ID
        assert result.audit_event.drone_id == DRONE_ID

    @pytest.mark.parametrize(
        ("src", "dst"),
        [(S.ASSIGNED, S.PICKED_UP), (S.IN_FLIGHT, S.ARRIVING), (S.CREATED, S.CANCELLED)],
    )
    def test_a_drone_id_is_rejected_for_every_other_target(
        self, machine: OrderStateMachine, make_order: MakeOrder, src: OrderStatus, dst: OrderStatus
    ) -> None:
        with pytest.raises(OrderValidationError):
            machine.transition(
                make_order(src), dst, source=EventSource.DISPATCH, drone_id=UUID(int=0xBAD)
            )


class TestNoDirectStatusUpdates:
    """Rule 4 at the domain level: status cannot be assigned, only derived."""

    def test_status_cannot_be_assigned(self, make_order: MakeOrder) -> None:
        order = make_order(S.CREATED)
        with pytest.raises(FrozenInstanceError):
            order.status = S.DELIVERED  # type: ignore[misc]
        assert order.status is S.CREATED

    def test_status_cannot_be_assigned_via_setattr(self, make_order: MakeOrder) -> None:
        order = make_order(S.CREATED)
        with pytest.raises(FrozenInstanceError):
            setattr(order, "status", S.DELIVERED)  # noqa: B010
        assert order.status is S.CREATED

    def test_a_transition_result_cannot_be_modified(
        self, machine: OrderStateMachine, make_order: MakeOrder
    ) -> None:
        result = machine.transition(make_order(S.CREATED), S.CANCELLED, source=EventSource.API)
        with pytest.raises(FrozenInstanceError):
            result.order = make_order(S.CREATED)  # type: ignore[misc]


class TestClockAndIdInjection:
    def test_default_machine_uses_aware_utc_time_and_random_ids(
        self, pickup: Location, dropoff: Location, package: Package
    ) -> None:
        result = OrderStateMachine().create(
            client_id="mock",
            external_order_id="ORD-1",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=EventSource.API,
        )
        assert result.order.created_at.utcoffset() is not None
        assert isinstance(result.order.id, UUID)
        assert result.order.id != result.audit_event.event_id
