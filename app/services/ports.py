"""Persistence ports used by the services.

Deliberately narrow. There is **no** generic ``save(order)`` or
``update_status(...)``: the only write operations accept a ``TransitionResult``,
which can only be produced by the order state machine and always carries its
audit event (Rules 4 and 5).
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.domain.models import AuditEvent, Order
from app.domain.order_state_machine import TransitionResult


class OrderStore(Protocol):
    async def get(self, order_id: UUID, *, for_update: bool = False) -> Order | None:
        """Load an order. ``for_update`` locks the row until the unit of work ends."""
        ...

    async def get_by_external_id(self, client_id: str, external_order_id: str) -> Order | None: ...

    async def insert(self, result: TransitionResult) -> bool:
        """Store a newly created order together with its audit event.

        Returns ``False`` (and stores nothing) if an order with the same
        ``(client_id, external_order_id)`` already exists.
        """
        ...

    async def apply_transition(self, result: TransitionResult) -> None:
        """Store a status change together with its audit event.

        Raises ``ConcurrentUpdateError`` if the stored status is no longer the
        event's ``previous_state``.
        """
        ...


class AuditLog(Protocol):
    """Read access to the append-only audit trail."""

    async def list_for_order(self, order_id: UUID) -> list[AuditEvent]:
        """All events for an order, oldest first."""
        ...


class UnitOfWork(Protocol):
    """One transaction. Leaving the block without ``commit()`` rolls everything back."""

    # Read-only properties (not mutable attributes), so an implementation may expose
    # narrower concrete types, e.g. ``orders: SqlAlchemyOrderStore``.
    @property
    def orders(self) -> OrderStore: ...

    @property
    def audit(self) -> AuditLog: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
