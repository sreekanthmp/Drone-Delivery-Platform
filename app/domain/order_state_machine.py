"""The order state machine (Rules 4 and 5).

This is the single choke point for every order status value, including the
initial one. It is a pure "functional core":

* ``create`` and ``transition`` never mutate anything. They return a
  :class:`TransitionResult` holding the new :class:`Order` **and** the
  :class:`AuditEvent` describing the change, so a status change can never
  exist without its audit record.
* Invalid changes raise domain exceptions before anything is produced.
* There is no I/O. The caller (the order service, Phase 2) is responsible for
  persisting the order and the event together in one transaction.

Allowed transitions::

    created -> assigned -> picked_up -> in_flight -> arriving -> delivered
    any active state -> failed
    any active state -> cancelled
    in_flight -> returned
    arriving  -> returned

``delivered``, ``failed``, ``cancelled`` and ``returned`` are terminal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.exceptions import (
    InconsistentTransitionResultError,
    InvalidTransitionError,
    MissingTransitionContextError,
    OrderValidationError,
    TerminalStateError,
)
from app.domain.models.audit import AuditEvent, AuditEventType, EventSource
from app.domain.models.order import (
    ACTIVE_STATES,
    Location,
    Order,
    OrderStatus,
    Package,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]

_FORWARD_PATH: Final[Mapping[OrderStatus, OrderStatus]] = MappingProxyType(
    {
        OrderStatus.CREATED: OrderStatus.ASSIGNED,
        OrderStatus.ASSIGNED: OrderStatus.PICKED_UP,
        OrderStatus.PICKED_UP: OrderStatus.IN_FLIGHT,
        OrderStatus.IN_FLIGHT: OrderStatus.ARRIVING,
        OrderStatus.ARRIVING: OrderStatus.DELIVERED,
    }
)
_RETURNABLE: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.IN_FLIGHT, OrderStatus.ARRIVING}
)


def _build_transition_table() -> Mapping[OrderStatus, frozenset[OrderStatus]]:
    table: dict[OrderStatus, set[OrderStatus]] = {status: set() for status in OrderStatus}
    for source, target in _FORWARD_PATH.items():
        table[source].add(target)
    for status in ACTIVE_STATES:
        table[status] |= {OrderStatus.FAILED, OrderStatus.CANCELLED}
    for status in _RETURNABLE:
        table[status].add(OrderStatus.RETURNED)
    return MappingProxyType({status: frozenset(targets) for status, targets in table.items()})


ALLOWED_TRANSITIONS: Final[Mapping[OrderStatus, frozenset[OrderStatus]]] = _build_transition_table()
"""For each status, the set of statuses it may move to."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """The outcome of a successful ``create`` or ``transition``.

    Persistence accepts this type as proof that a status change went through the
    state machine, so it refuses to exist in an inconsistent form: the audit event
    must describe exactly this order, and a status change must be an allowed edge.
    """

    order: Order
    audit_event: AuditEvent

    def __post_init__(self) -> None:
        order, event = self.order, self.audit_event
        if (
            event.order_id != order.id
            or event.new_state is not order.status
            or event.drone_id != order.drone_id
        ):
            raise InconsistentTransitionResultError(
                "the audit event does not describe the resulting order"
            )
        if (
            event.event_type is AuditEventType.ORDER_STATUS_CHANGED
            and event.previous_state is not None
            and event.new_state is not None
            and event.new_state not in ALLOWED_TRANSITIONS[event.previous_state]
        ):
            raise InvalidTransitionError(event.previous_state, event.new_state)


class OrderStateMachine:
    """Validates and performs order lifecycle changes.

    ``clock`` and ``id_factory`` are injectable so tests are deterministic.
    The machine holds no order state of its own.
    """

    def __init__(
        self,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory

    @staticmethod
    def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[current]

    @staticmethod
    def allowed_targets(current: OrderStatus) -> frozenset[OrderStatus]:
        return ALLOWED_TRANSITIONS[current]

    def create(
        self,
        *,
        client_id: str,
        external_order_id: str,
        pickup: Location,
        dropoff: Location,
        package: Package,
        source: EventSource,
        metadata: Mapping[str, Any] | None = None,
    ) -> TransitionResult:
        """Create a new order in ``created`` together with its audit event."""
        now = self._clock()
        order = Order(
            id=self._id_factory(),
            client_id=client_id,
            external_order_id=external_order_id,
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            status=OrderStatus.CREATED,
            created_at=now,
            updated_at=now,
        )
        event = AuditEvent(
            event_id=self._id_factory(),
            order_id=order.id,
            event_type=AuditEventType.ORDER_CREATED,
            previous_state=None,
            new_state=OrderStatus.CREATED,
            occurred_at=now,
            source=source,
            drone_id=None,
            metadata=metadata or {},
        )
        return TransitionResult(order=order, audit_event=event)

    def transition(
        self,
        order: Order,
        target: OrderStatus,
        *,
        source: EventSource,
        drone_id: UUID | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TransitionResult:
        """Move ``order`` to ``target``.

        ``drone_id`` is required when moving to ``assigned`` and rejected for
        every other target.

        Raises:
            TerminalStateError: the order is already in a terminal state.
            InvalidTransitionError: the edge is not in the transition table
                (this includes moving to the current state).
            MissingTransitionContextError: moving to ``assigned`` without a
                ``drone_id``.
            OrderValidationError: a ``drone_id`` was given for any target other
                than ``assigned``.
        """
        if not self.can_transition(order.status, target):
            if order.status.is_terminal:
                raise TerminalStateError(order.status, target)
            raise InvalidTransitionError(order.status, target)

        if target is OrderStatus.ASSIGNED:
            if drone_id is None:
                raise MissingTransitionContextError("moving to 'assigned' requires a drone_id")
            new_drone_id: UUID | None = drone_id
        elif drone_id is not None:
            raise OrderValidationError(
                f"drone_id can only be supplied when moving to 'assigned', not '{target}'"
            )
        else:
            new_drone_id = order.drone_id

        now = self._clock()
        new_order = replace(order, status=target, drone_id=new_drone_id, updated_at=now)
        event = AuditEvent(
            event_id=self._id_factory(),
            order_id=order.id,
            event_type=AuditEventType.ORDER_STATUS_CHANGED,
            previous_state=order.status,
            new_state=target,
            occurred_at=now,
            source=source,
            drone_id=new_order.drone_id,
            metadata=metadata or {},
        )
        return TransitionResult(order=new_order, audit_event=event)
