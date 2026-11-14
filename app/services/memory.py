"""An in-memory unit of work: the whole persistence layer without a database.

Used by fast service and API tests and by the one-command demo (``python -m app.demo``). It
mirrors the contract of the SQLAlchemy implementation that matters to callers: atomic commit
(nothing is visible until ``commit()``; leaving the block without committing discards it), the
``(client_id, external_order_id)`` uniqueness, and the compare-and-set on the previous status.
The service contract tests run the same assertions against both implementations to keep this
one honest.

It is a stand-in for development and tests, not a store to run a business on: nothing survives
the process, and it has none of the database's own guards (the lifecycle triggers).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from app.domain.flight_state_machine import FlightTransitionResult
from app.domain.models import (
    AuditEvent,
    AuditEventType,
    DeliveryAttempt,
    DeliveryStatus,
    Drone,
    DroneStatus,
    Flight,
    FlightStatus,
    Order,
    WebhookDelivery,
)
from app.domain.models.drone import check_status_change
from app.domain.order_state_machine import TransitionResult
from app.services.exceptions import ConcurrentUpdateError


@dataclass
class InMemoryDatabase:
    orders: dict[UUID, Order] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    drones: dict[UUID, Drone] = field(default_factory=dict)
    flights: dict[UUID, Flight] = field(default_factory=dict)
    deliveries: dict[UUID, WebhookDelivery] = field(default_factory=dict)
    delivery_attempts: list[DeliveryAttempt] = field(default_factory=list)
    commits: int = 0


class _WebhookStore:
    def __init__(
        self, deliveries: dict[UUID, WebhookDelivery], attempts: list[DeliveryAttempt]
    ) -> None:
        self._deliveries = deliveries
        self._attempts = attempts

    async def add(self, event: AuditEvent, client_id: str) -> bool:
        if event.event_id in self._deliveries:
            return False
        delivery = WebhookDelivery.queue(event, client_id)
        self._deliveries[event.event_id] = replace(delivery, seq=len(self._deliveries) + 1)
        return True

    async def claim_due(
        self, *, now: datetime, lease_until: datetime, limit: int
    ) -> list[WebhookDelivery]:
        pending = sorted(
            (d for d in self._deliveries.values() if d.status is DeliveryStatus.PENDING),
            key=lambda d: d.seq or 0,
        )
        claimed: list[WebhookDelivery] = []
        for delivery in pending:
            if len(claimed) >= limit:
                break
            if delivery.next_attempt_at > now:
                continue
            if delivery.lease_until is not None and delivery.lease_until > now:
                continue
            # An order's updates go out one at a time, oldest first.
            if any(
                earlier.order_id == delivery.order_id
                and earlier.client_id == delivery.client_id
                and (earlier.seq or 0) < (delivery.seq or 0)
                for earlier in pending
            ):
                continue
            taken = delivery.claimed(until=lease_until, now=now)
            self._deliveries[delivery.event_id] = taken
            claimed.append(taken)
        return claimed

    async def apply(
        self, before: WebhookDelivery, after: WebhookDelivery, attempt: DeliveryAttempt | None
    ) -> bool:
        current = self._deliveries.get(before.event_id)
        if current is None or current.status is not before.status:
            return False
        if current.attempts != before.attempts:
            return False
        self._deliveries[before.event_id] = replace(after, seq=current.seq)
        if attempt is not None:
            self._attempts.append(attempt)
        return True

    async def get(self, event_id: UUID) -> WebhookDelivery | None:
        return self._deliveries.get(event_id)

    async def list_recent(
        self,
        *,
        status: DeliveryStatus | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        matching = [
            d
            for d in self._deliveries.values()
            if (status is None or d.status is status)
            and (client_id is None or d.client_id == client_id)
        ]
        return sorted(matching, key=lambda d: d.seq or 0, reverse=True)[:limit]

    async def list_for_order(self, order_id: UUID) -> list[WebhookDelivery]:
        mine = [d for d in self._deliveries.values() if d.order_id == order_id]
        return sorted(mine, key=lambda d: d.seq or 0)

    async def attempts_for(self, event_id: UUID) -> list[DeliveryAttempt]:
        mine = [a for a in self._attempts if a.event_id == event_id]
        return sorted(mine, key=lambda a: a.attempt_no)


class _OrderStore:
    def __init__(
        self, orders: dict[UUID, Order], audit: list[AuditEvent], webhooks: _WebhookStore
    ) -> None:
        self._orders = orders
        self._audit = audit
        self._webhooks = webhooks

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
        await self._webhooks.add(result.audit_event, order.client_id)
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
        await self._webhooks.add(event, order.client_id)


class _AuditLog:
    def __init__(self, audit: list[AuditEvent]) -> None:
        self._audit = audit

    async def get(self, event_id: UUID) -> AuditEvent | None:
        return next((event for event in self._audit if event.event_id == event_id), None)

    async def list_for_order(self, order_id: UUID) -> list[AuditEvent]:
        return [event for event in self._audit if event.order_id == order_id]

    async def list_for_flight(self, flight_id: UUID) -> list[AuditEvent]:
        return [event for event in self._audit if event.flight_id == flight_id]


class _DroneStore:
    def __init__(self, drones: dict[UUID, Drone]) -> None:
        self._drones = drones

    async def add(self, drone: Drone) -> bool:
        if any(existing.name == drone.name for existing in self._drones.values()):
            return False
        self._drones[drone.id] = drone
        return True

    async def get(self, drone_id: UUID, *, for_update: bool = False) -> Drone | None:
        return self._drones.get(drone_id)

    async def list_all(self, *, status: DroneStatus | None = None) -> list[Drone]:
        drones = [d for d in self._drones.values() if status is None or d.status is status]
        return sorted(drones, key=lambda d: d.name)

    async def list_available(self, *, lock: bool = False) -> list[Drone]:
        drones = [d for d in self._drones.values() if d.status is DroneStatus.AVAILABLE]
        return sorted(drones, key=lambda d: d.id)

    async def set_battery(self, drone_id: UUID, battery_percent: float, *, now: datetime) -> bool:
        drone = self._drones.get(drone_id)
        if drone is None:
            return False
        self._drones[drone_id] = drone.with_battery(battery_percent, now=now)
        return True

    async def apply_status_change(self, before: Drone, after: Drone) -> None:
        check_status_change(before, after)
        current = self._drones.get(before.id)
        if current is None or current.status is not before.status:
            raise ConcurrentUpdateError(f"drone {before.id} is not in state {before.status}")
        self._drones[before.id] = after


class _FlightStore:
    def __init__(self, flights: dict[UUID, Flight], audit: list[AuditEvent]) -> None:
        self._flights = flights
        self._audit = audit

    async def get(self, flight_id: UUID, *, for_update: bool = False) -> Flight | None:
        return self._flights.get(flight_id)

    async def list_for_order(self, order_id: UUID) -> list[Flight]:
        flights = [f for f in self._flights.values() if f.order_id == order_id]
        return sorted(flights, key=lambda f: (f.created_at, f.id))

    async def get_live_for_drone(self, drone_id: UUID) -> Flight | None:
        return next(
            (f for f in self._flights.values() if f.drone_id == drone_id and f.status.is_live),
            None,
        )

    async def list_live(self) -> list[Flight]:
        live = [f for f in self._flights.values() if f.status.is_live]
        return sorted(live, key=lambda f: (f.created_at, f.id))

    async def list_stale_preflight(self, updated_before: datetime) -> list[Flight]:
        preflight = {
            FlightStatus.PLANNED,
            FlightStatus.PERMISSION_GRANTED,
            FlightStatus.MISSION_UPLOADED,
        }
        stale = [
            f
            for f in self._flights.values()
            if f.status in preflight and f.updated_at < updated_before
        ]
        return sorted(stale, key=lambda f: (f.updated_at, f.id))

    async def insert(self, result: FlightTransitionResult) -> bool:
        if result.audit_event.event_type is not AuditEventType.FLIGHT_CREATED:
            raise ValueError("insert() needs a result produced by FlightStateMachine.create()")
        flight = result.flight
        for other in self._flights.values():
            if other.status.is_live and (
                other.drone_id == flight.drone_id or other.order_id == flight.order_id
            ):
                return False
        self._flights[flight.id] = flight
        self._audit.append(result.audit_event)
        return True

    async def apply_transition(self, result: FlightTransitionResult) -> None:
        flight, event = result.flight, result.audit_event
        if event.event_type is not AuditEventType.FLIGHT_STATUS_CHANGED:
            raise ValueError("apply_transition() needs a result produced by transition()")
        current = self._flights.get(flight.id)
        if current is None or current.status is not event.previous_state:
            raise ConcurrentUpdateError(
                f"flight {flight.id} is not in state {event.previous_state}"
            )
        self._flights[flight.id] = flight
        self._audit.append(event)


class InMemoryUnitOfWork:
    orders: _OrderStore
    audit: _AuditLog
    drones: _DroneStore
    flights: _FlightStore
    webhooks: _WebhookStore

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database
        self._staged_orders: dict[UUID, Order] = {}
        self._staged_audit: list[AuditEvent] = []
        self._staged_drones: dict[UUID, Drone] = {}
        self._staged_flights: dict[UUID, Flight] = {}
        self._staged_deliveries: dict[UUID, WebhookDelivery] = {}
        self._staged_attempts: list[DeliveryAttempt] = []

    async def __aenter__(self) -> Self:
        self._staged_orders = dict(self._database.orders)
        self._staged_audit = list(self._database.audit)
        self._staged_drones = dict(self._database.drones)
        self._staged_flights = dict(self._database.flights)
        self._staged_deliveries = dict(self._database.deliveries)
        self._staged_attempts = list(self._database.delivery_attempts)
        self.webhooks = _WebhookStore(self._staged_deliveries, self._staged_attempts)
        self.orders = _OrderStore(self._staged_orders, self._staged_audit, self.webhooks)
        self.audit = _AuditLog(self._staged_audit)
        self.drones = _DroneStore(self._staged_drones)
        self.flights = _FlightStore(self._staged_flights, self._staged_audit)
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
        self._staged_drones = {}
        self._staged_flights = {}
        self._staged_deliveries = {}
        self._staged_attempts = []

    async def commit(self) -> None:
        self._database.orders = dict(self._staged_orders)
        self._database.audit = list(self._staged_audit)
        self._database.drones = dict(self._staged_drones)
        self._database.flights = dict(self._staged_flights)
        self._database.deliveries = dict(self._staged_deliveries)
        self._database.delivery_attempts = list(self._staged_attempts)
        self._database.commits += 1
