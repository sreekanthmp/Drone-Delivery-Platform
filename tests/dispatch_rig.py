"""A ready-made dispatch stack for tests: every service and fake wired around one unit of work.

``build_rig(uow_factory)`` gives the real ``OrderService`` and ``DispatchService`` over any
unit-of-work factory (in-memory or PostgreSQL), the mock compliance provider and the
in-memory drone link. ``assert_rule2`` is the invariant every dispatch scenario must keep.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.compliance.mock_digital_sky import MockDigitalSky
from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.service import DispatchService
from app.domain.models import (
    AuditEvent,
    AuditEventType,
    Drone,
    EventSource,
    Flight,
    FlightStatus,
    Location,
    Order,
    OrderStatus,
    Package,
)
from app.domain.order_state_machine import OrderStateMachine
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.conftest import DROPOFF, HOME, PICKUP

Clock = Callable[[], datetime]


def real_clock() -> datetime:
    return datetime.now(UTC)


class ScriptedCompliance(MockDigitalSky):
    """The mock provider, with a hook that runs before each re-validation.

    ``on_validate(n, permission_id)`` is called with the 1-based count of validations; it
    may call ``revoke`` or ``set_outage``. In a dispatch, validation 1 is the check right
    after the grant and validation 2 is the re-check that feeds the start gate.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.validations = 0
        self.on_validate: Callable[[int, str], None] | None = None

    async def validate_permission(self, permission_id: str) -> Any:
        self.validations += 1
        if self.on_validate is not None:
            self.on_validate(self.validations, permission_id)
        return await super().validate_permission(permission_id)


class HookedLink(InMemoryDroneLink):
    """The in-memory link, with an async hook that runs while an upload is in progress."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.on_upload: Callable[[], Awaitable[None]] | None = None

    async def upload_mission(self, drone_id: UUID, mission: Any) -> Any:
        if self.on_upload is not None:
            await self.on_upload()
        return await super().upload_mission(drone_id, mission)


class HookedOrderService(OrderService):
    """An ``OrderService`` that can run something just before the order is assigned."""

    before_assign: Callable[[], Awaitable[None]] | None = None

    async def transition(self, order_id: UUID, target: OrderStatus, **kwargs: Any) -> Order:
        if target is OrderStatus.ASSIGNED and self.before_assign is not None:
            hook, self.before_assign = self.before_assign, None  # once
            await hook()
        return await super().transition(order_id, target, **kwargs)


@dataclass
class Rig:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    orders: HookedOrderService
    compliance: ScriptedCompliance
    link: HookedLink
    dispatch: DispatchService
    created: list[Order] = field(default_factory=list)
    _counter: int = 0

    # --- arrange ---------------------------------------------------------------------------

    async def add_drone(self, name: str | None = None, **overrides: Any) -> Drone:
        self._counter += 1
        fields: dict[str, Any] = {
            "name": name or f"drone-{self._counter}",
            "home": HOME,
            "max_payload_kg": 5.0,
            "max_range_km": 20.0,
            "battery_percent": 90.0,
        }
        fields.update(overrides)
        return await self.dispatch.register_drone(**fields)

    async def new_order(
        self,
        external_id: str | None = None,
        *,
        weight_kg: float = 1.5,
        pickup: Location = PICKUP,
        dropoff: Location = DROPOFF,
    ) -> Order:
        self._counter += 1
        result = await self.orders.create_order(
            client_id="mock",
            external_order_id=external_id or f"ORD-{self._counter}",
            pickup=pickup,
            dropoff=dropoff,
            package=Package(weight_kg=weight_kg, description="meals"),
            source=EventSource.API,
        )
        self.created.append(result.order)
        return result.order

    # --- observe ---------------------------------------------------------------------------

    async def order(self, order_id: UUID) -> Order:
        return await self.orders.get_order(order_id)

    async def drone(self, drone_id: UUID) -> Drone:
        return await self.dispatch.get_drone(drone_id)

    async def flights(self, order_id: UUID) -> list[Flight]:
        return await self.dispatch.list_flights_for_order(order_id)

    async def only_flight(self, order_id: UUID) -> Flight:
        flights = await self.flights(order_id)
        assert len(flights) == 1, flights
        return flights[0]

    async def trail(self, order_id: UUID) -> list[AuditEvent]:
        return await self.orders.get_audit_trail(order_id)

    async def steps(self, order_id: UUID) -> list[tuple[str, str]]:
        """The audit trail as ``(kind, new state)`` pairs, oldest first."""
        kinds = {
            AuditEventType.ORDER_CREATED: "order created",
            AuditEventType.ORDER_STATUS_CHANGED: "order",
            AuditEventType.FLIGHT_CREATED: "flight created",
            AuditEventType.FLIGHT_STATUS_CHANGED: "flight",
        }
        return [(kinds[e.event_type], e.new_state.value) for e in await self.trail(order_id)]

    def commands(self) -> list[str]:
        """What reached a drone, oldest first, as operation names."""
        return [operation for operation, _ in self.link.commands]


def build_rig(
    uow_factory: UnitOfWorkFactory,
    *,
    clock: Clock = real_clock,
    validity: timedelta = timedelta(hours=1),
) -> Rig:
    # A typical sortie is estimated at ~22 minutes, so the mock's permissions must outlast
    # that (the composition root does the same from settings).
    orders = HookedOrderService(uow_factory, machine=OrderStateMachine(clock=clock))
    compliance = ScriptedCompliance(clock=clock, validity=validity)
    link = HookedLink(clock=clock)
    dispatch = DispatchService(
        uow_factory, orders=orders, compliance=compliance, link=link, clock=clock
    )
    return Rig(uow_factory, clock, orders, compliance, link, dispatch)


async def assert_rule2(rig: Rig) -> None:
    """Rule 2, as an invariant over everything that happened.

    * a ``start`` that reached a drone was preceded by a recorded, gated start of a flight
      of that drone (the gate runs before the command, never after);
    * every flight that ever started had a granted permission and a mission the drone
      acknowledged by checksum.
    """
    allowed: Counter[UUID] = Counter()  # drone -> recorded starts (first starts and resumes)
    for order in rig.created:
        events = await rig.trail(order.id)
        for flight in await rig.flights(order.id):
            started = [
                e
                for e in events
                if e.flight_id == flight.id
                and e.event_type is AuditEventType.FLIGHT_STATUS_CHANGED
                and e.new_state is FlightStatus.STARTED
            ]
            if started:
                assert flight.permission is not None, f"flight {flight.id} started unpermitted"
                assert flight.acknowledged_checksum == flight.mission.checksum, (
                    f"flight {flight.id} started with an unacknowledged mission"
                )
            allowed[flight.drone_id] += len(started)

    sent: Counter[UUID] = Counter(rig.link.operations("start"))
    for drone_id, count in sent.items():
        assert count <= allowed[drone_id], (
            f"drone {drone_id} received {count} start command(s) but only "
            f"{allowed[drone_id]} gated start(s) were recorded"
        )
