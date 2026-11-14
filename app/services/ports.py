"""Persistence ports used by the services.

Deliberately narrow. There is **no** generic ``save(order)`` or
``update_status(...)``: the only write operations accept a ``TransitionResult``,
which can only be produced by the order state machine and always carries its
audit event (Rules 4 and 5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.domain.flight_state_machine import FlightTransitionResult
from app.domain.models import (
    AuditEvent,
    DeliveryAttempt,
    DeliveryStatus,
    Drone,
    DroneStatus,
    Flight,
    Order,
    WebhookDelivery,
)
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

    async def get(self, event_id: UUID) -> AuditEvent | None: ...

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

    async def set_battery(self, drone_id: UUID, battery_percent: float, *, now: datetime) -> bool:
        """Store a new battery level. ``False`` if there is no such drone."""
        ...


class FlightStore(Protocol):
    """Flights. Like orders, the only writes take a ``FlightTransitionResult``."""

    async def get(self, flight_id: UUID, *, for_update: bool = False) -> Flight | None: ...

    async def list_for_order(self, order_id: UUID) -> list[Flight]:
        """Every flight of an order, oldest first."""
        ...

    async def get_live_for_drone(self, drone_id: UUID) -> Flight | None:
        """The drone's live flight, if it has one (there is at most one)."""
        ...

    async def list_live(self) -> list[Flight]:
        """Every flight that is not finished yet, oldest first."""
        ...

    async def list_stale_preflight(self, updated_before: datetime) -> list[Flight]:
        """Flights still in ``planned``, ``permission_granted`` or ``mission_uploaded`` whose last
        change is older than ``updated_before``, oldest first."""
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


class WebhookStore(Protocol):
    """Webhook deliveries (Phase 6): the queue of notifications owed to partners.

    ``add`` is called only by the order stores, in the same transaction as the order's audit
    event, so an order change cannot exist without its notification. The sender claims due
    deliveries and records what happened; nothing here changes an order.
    """

    async def add(self, event: AuditEvent, client_id: str) -> bool:
        """Queue the notification for an order audit event.

        Returns ``False`` (storing nothing) if that event is already queued.
        """
        ...

    async def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int
    ) -> list[WebhookDelivery]:
        """Claim up to ``limit`` deliveries that are due, oldest first, until ``lease_until``.

        Due means pending, ``next_attempt_at`` reached, not leased by someone else, and no
        earlier delivery of the same order (to the same client) still pending: an order's
        updates go out in order. A delivery another transaction has locked is skipped, so
        concurrent senders claim different ones.
        """
        ...

    async def apply(
        self, before: WebhookDelivery, after: WebhookDelivery, attempt: DeliveryAttempt | None
    ) -> bool:
        """Store ``after`` (and the attempt, if one was made), if the delivery is still ``before``.

        Compare-and-set on the status and the attempt count. Returns ``False`` (storing
        nothing) when something else changed it first, for example a sender whose lease expired.
        """
        ...

    async def get(self, event_id: UUID) -> WebhookDelivery | None: ...

    async def list_recent(
        self,
        *,
        status: DeliveryStatus | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        """Deliveries, newest first, optionally of one status and/or one client."""
        ...

    async def list_for_order(self, order_id: UUID) -> list[WebhookDelivery]:
        """Every delivery of an order, oldest first."""
        ...

    async def attempts_for(self, event_id: UUID) -> list[DeliveryAttempt]:
        """Every HTTP attempt at a delivery, oldest first."""
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

    @property
    def webhooks(self) -> WebhookStore: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class AuditNotifier(Protocol):
    """Told about audit events *after* they have been committed (a live view of the trail).

    Best-effort: implementations must not raise, and a service calls this only once the
    change is durable, so a failure here can never undo or block a status change.
    """

    async def notify(self, events: Sequence[AuditEvent]) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
