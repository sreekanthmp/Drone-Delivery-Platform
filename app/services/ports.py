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

from app.domain.flight_state_machine import FlightTransitionResult
from app.domain.models import AuditEvent, Drone, DroneStatus, Flight, Order
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
        """All events for an order (order and flight events), oldest first."""
        ...

    async def list_for_flight(self, flight_id: UUID) -> list[AuditEvent]:
        """All events for a flight, oldest first."""
        ...


class DroneStore(Protocol):
    """The fleet registry. Status changes are compare-and-set on the previous status."""

    async def add(self, drone: Drone) -> bool:
        """Register a drone. Returns ``False`` (storing nothing) if the name is taken."""
        ...

    async def get(self, drone_id: UUID, *, for_update: bool = False) -> Drone | None: ...

    async def list_all(self, *, status: DroneStatus | None = None) -> list[Drone]: ...

    async def list_available(self, *, lock: bool = False) -> list[Drone]:
        """Drones that may take a delivery. ``lock`` locks them, skipping rows another
        transaction has locked, so concurrent dispatchers pick different drones."""
        ...

    async def apply_status_change(self, before: Drone, after: Drone) -> None:
        """Store ``after`` (``before`` with a legal status change applied).

        Raises ``ConcurrentUpdateError`` if the stored status is no longer ``before``'s.
        """
        ...


class FlightStore(Protocol):
    """Flights. Like orders, the only writes take a ``FlightTransitionResult``."""

    async def get(self, flight_id: UUID, *, for_update: bool = False) -> Flight | None: ...

    async def list_for_order(self, order_id: UUID) -> list[Flight]:
        """Every flight of an order, oldest first."""
        ...

    async def insert(self, result: FlightTransitionResult) -> bool:
        """Store a newly planned flight together with its audit event.

        Returns ``False`` (storing nothing) if the drone or the order already has a live
        flight.
        """
        ...

    async def apply_transition(self, result: FlightTransitionResult) -> None:
        """Store a flight status change together with its audit event.

        Raises ``ConcurrentUpdateError`` if the stored status is no longer the event's
        ``previous_state``.
        """
        ...


class UnitOfWork(Protocol):
    """One transaction. Leaving the block without ``commit()`` rolls everything back."""

    # Read-only properties (not mutable attributes), so an implementation may expose
    # narrower concrete types, e.g. ``orders: SqlAlchemyOrderStore``.
    @property
    def orders(self) -> OrderStore: ...

    @property
    def audit(self) -> AuditLog: ...

    @property
    def drones(self) -> DroneStore: ...

    @property
    def flights(self) -> FlightStore: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
