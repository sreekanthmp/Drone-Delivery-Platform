"""In-memory unit of work for fast service and API tests (no database needed).

It mirrors the contract of the SQLAlchemy implementation that matters to callers:
atomic commit (nothing is visible until ``commit()``; leaving the block without
committing discards it), the ``(client_id, external_order_id)`` uniqueness, and
the compare-and-set on the previous status. The service contract tests run the
same assertions against both implementations to keep the fake honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import UUID

from app.domain.models import AuditEvent, AuditEventType, Order
from app.domain.order_state_machine import TransitionResult
from app.services.exceptions import ConcurrentUpdateError


@dataclass
class InMemoryDatabase:
    orders: dict[UUID, Order] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    commits: int = 0


class _OrderStore:
    def __init__(self, orders: dict[UUID, Order], audit: list[AuditEvent]) -> None:
        self._orders = orders
        self._audit = audit

    async def get(self, order_id: UUID, *, for_update: bool = False) -> Order | None:
        return self._orders.get(order_id)

    async def get_by_external_id(self, client_id: str, external_order_id: str) -> Order | None:
        return next(
            (
                order
                for order in self._orders.values()
                if order.client_id == client_id and order.external_order_id == external_order_id
            ),
            None,
        )

    async def insert(self, result: TransitionResult) -> bool:
        if result.audit_event.event_type is not AuditEventType.ORDER_CREATED:
            raise ValueError("insert() needs a result produced by OrderStateMachine.create()")
        order = result.order
        if await self.get_by_external_id(order.client_id, order.external_order_id) is not None:
            return False
        self._orders[order.id] = order
        self._audit.append(result.audit_event)
        return True

    async def apply_transition(self, result: TransitionResult) -> None:
        order, event = result.order, result.audit_event
        if event.event_type is not AuditEventType.ORDER_STATUS_CHANGED:
            raise ValueError("apply_transition() needs a result produced by transition()")
        current = self._orders.get(order.id)
        if current is None or current.status is not event.previous_state:
            raise ConcurrentUpdateError(f"order {order.id} is not in state {event.previous_state}")
        self._orders[order.id] = order
        self._audit.append(event)


class _AuditLog:
    def __init__(self, audit: list[AuditEvent]) -> None:
        self._audit = audit

    async def list_for_order(self, order_id: UUID) -> list[AuditEvent]:
        return [event for event in self._audit if event.order_id == order_id]


class InMemoryUnitOfWork:
    orders: _OrderStore
    audit: _AuditLog

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database
        self._staged_orders: dict[UUID, Order] = {}
        self._staged_audit: list[AuditEvent] = []

    async def __aenter__(self) -> Self:
        self._staged_orders = dict(self._database.orders)
        self._staged_audit = list(self._database.audit)
        self.orders = _OrderStore(self._staged_orders, self._staged_audit)
        self.audit = _AuditLog(self._staged_audit)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Anything not committed is simply dropped with the staged copies.
        self._staged_orders = {}
        self._staged_audit = []

    async def commit(self) -> None:
        self._database.orders = dict(self._staged_orders)
        self._database.audit = list(self._staged_audit)
        self._database.commits += 1
