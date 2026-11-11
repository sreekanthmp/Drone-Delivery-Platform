"""OrderService contract, run against BOTH unit-of-work implementations.

The ``memory`` variant always runs. The ``postgres`` variant needs PostgreSQL with
PostGIS and is skipped without it. Running identical assertions against both keeps
the in-memory fake honest.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import (
    InvalidTransitionError,
    MissingTransitionContextError,
    OrderValidationError,
    TerminalStateError,
)
from app.domain.models import (
    AuditEventType,
    EventSource,
    Location,
    OrderStatus,
    Package,
)
from app.services.exceptions import OrderNotFoundError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

S = OrderStatus
DRONE = UUID(int=0xD0)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def uow_factory(request: pytest.FixtureRequest) -> UnitOfWorkFactory:
    if request.param == "memory":
        database = InMemoryDatabase()
        return lambda: InMemoryUnitOfWork(database)
    factory: UnitOfWorkFactory = request.getfixturevalue("pg_uow_factory")
    return factory


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory) -> OrderService:
    return OrderService(uow_factory)


@pytest.fixture
def new_order(pickup: Location, dropoff: Location, package: Package) -> dict[str, Any]:
    return {
        "client_id": "mock",
        "external_order_id": "ORD-1",
        "pickup": pickup,
        "dropoff": dropoff,
        "package": package,
        "source": EventSource.API,
    }


async def _walk(service: OrderService, order_id: UUID, *targets: OrderStatus) -> None:
    for target in targets:
        extra = {"drone_id": DRONE} if target is S.ASSIGNED else {}
        await service.transition(order_id, target, source=EventSource.DISPATCH, **extra)


class TestCreateOrder:
    async def test_stores_the_order_in_created_state(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        result = await service.create_order(**new_order)

        assert result.created is True
        assert result.order.status is S.CREATED
        assert result.order.drone_id is None
        assert await service.get_order(result.order.id) == result.order

    async def test_round_trips_every_field(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        created = (await service.create_order(**new_order)).order
        loaded = await service.get_order(created.id)

        assert loaded.client_id == "mock"
        assert loaded.external_order_id == "ORD-1"
        assert loaded.pickup == new_order["pickup"]
        assert loaded.dropoff == new_order["dropoff"]
        assert loaded.package == new_order["package"]
        assert loaded.created_at == created.created_at
        assert loaded.updated_at == created.updated_at
        assert loaded.created_at.utcoffset() == UTC.utcoffset(None)

    async def test_writes_exactly_one_order_created_audit_event(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order, metadata={"channel": "test"})).order

        trail = await service.get_audit_trail(order.id)

        assert len(trail) == 1
        event = trail[0]
        assert event.event_type is AuditEventType.ORDER_CREATED
        assert event.order_id == order.id
        assert event.previous_state is None
        assert event.new_state is S.CREATED
        assert event.drone_id is None
        assert event.source is EventSource.API
        assert event.metadata == {"channel": "test"}
        assert event.occurred_at == order.created_at

    async def test_is_idempotent_on_client_and_external_order_id(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        first = await service.create_order(**new_order)
        second = await service.create_order(**{**new_order, "package": Package(weight_kg=9.9)})

        assert first.created is True
        assert second.created is False
        assert second.order.id == first.order.id
        assert second.order.package == new_order["package"], "the original order is returned as-is"
        assert len(await service.get_audit_trail(first.order.id)) == 1

    async def test_the_same_external_id_from_another_client_is_a_different_order(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        first = await service.create_order(**new_order)
        other = await service.create_order(**{**new_order, "client_id": "swiggy"})

        assert other.created is True
        assert other.order.id != first.order.id

    async def test_invalid_input_stores_nothing(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        with pytest.raises(OrderValidationError):
            await service.create_order(**{**new_order, "dropoff": new_order["pickup"]})

        retry = await service.create_order(**new_order)
        assert retry.created is True, "the failed attempt must not have reserved the key"


class TestTransition:
    async def test_applies_the_change_and_records_the_audit_event(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order

        updated = await service.transition(
            order.id,
            S.ASSIGNED,
            source=EventSource.DISPATCH,
            drone_id=DRONE,
            metadata={"reason": "nearest drone", "score": 0.93},
        )

        assert updated.status is S.ASSIGNED
        assert updated.drone_id == DRONE
        assert await service.get_order(order.id) == updated
        event = (await service.get_audit_trail(order.id))[-1]
        assert event.event_type is AuditEventType.ORDER_STATUS_CHANGED
        assert (event.previous_state, event.new_state) == (S.CREATED, S.ASSIGNED)
        assert event.drone_id == DRONE
        assert event.source is EventSource.DISPATCH
        assert event.metadata == {"reason": "nearest drone", "score": 0.93}

    async def test_full_lifecycle_leaves_a_complete_ordered_audit_trail(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order

        await _walk(
            service, order.id, S.ASSIGNED, S.PICKED_UP, S.IN_FLIGHT, S.ARRIVING, S.DELIVERED
        )

        final = await service.get_order(order.id)
        assert final.status is S.DELIVERED
        assert final.drone_id == DRONE
        trail = await service.get_audit_trail(order.id)
        assert [e.new_state for e in trail] == [
            S.CREATED,
            S.ASSIGNED,
            S.PICKED_UP,
            S.IN_FLIGHT,
            S.ARRIVING,
            S.DELIVERED,
        ]
        assert [e.previous_state for e in trail[1:]] == [e.new_state for e in trail[:-1]]
        assert len({e.event_id for e in trail}) == len(trail)
        assert [e.occurred_at for e in trail] == sorted(e.occurred_at for e in trail)

    async def test_nested_metadata_round_trips(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order
        metadata = {"a": {"b": [1, "two", None, {"c": True}]}, "n": 1.5}

        await service.transition(order.id, S.CANCELLED, source=EventSource.OPS, metadata=metadata)

        event = (await service.get_audit_trail(order.id))[-1]
        assert event.metadata == {"a": {"b": (1, "two", None, {"c": True})}, "n": 1.5}

    async def test_an_invalid_transition_raises_and_writes_nothing(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order

        with pytest.raises(InvalidTransitionError):
            await service.transition(order.id, S.DELIVERED, source=EventSource.API)

        assert (await service.get_order(order.id)).status is S.CREATED
        assert len(await service.get_audit_trail(order.id)) == 1

    async def test_terminal_orders_cannot_change(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order
        await service.transition(order.id, S.CANCELLED, source=EventSource.API)

        with pytest.raises(TerminalStateError):
            await service.transition(order.id, S.FAILED, source=EventSource.API)

        assert (await service.get_order(order.id)).status is S.CANCELLED
        assert len(await service.get_audit_trail(order.id)) == 2

    async def test_assigning_without_a_drone_is_rejected_and_writes_nothing(
        self, service: OrderService, new_order: dict[str, Any]
    ) -> None:
        order = (await service.create_order(**new_order)).order

        with pytest.raises(MissingTransitionContextError):
            await service.transition(order.id, S.ASSIGNED, source=EventSource.DISPATCH)

        assert (await service.get_order(order.id)).status is S.CREATED
        assert len(await service.get_audit_trail(order.id)) == 1

    @pytest.mark.parametrize("target", [S.FAILED, S.CANCELLED, S.RETURNED])
    async def test_failure_cancellation_and_return_keep_the_drone(
        self, service: OrderService, new_order: dict[str, Any], target: OrderStatus
    ) -> None:
        order = (await service.create_order(**new_order)).order
        await _walk(service, order.id, S.ASSIGNED, S.PICKED_UP, S.IN_FLIGHT)

        result = await service.transition(order.id, target, source=EventSource.DISPATCH)

        assert result.status is target
        assert (await service.get_order(order.id)).drone_id == DRONE
        assert (await service.get_audit_trail(order.id))[-1].drone_id == DRONE


class TestNotFound:
    async def test_get_order(self, service: OrderService) -> None:
        with pytest.raises(OrderNotFoundError) as exc_info:
            await service.get_order(UUID(int=404))
        assert exc_info.value.order_id == UUID(int=404)

    async def test_transition(self, service: OrderService) -> None:
        with pytest.raises(OrderNotFoundError):
            await service.transition(UUID(int=404), S.CANCELLED, source=EventSource.API)

    async def test_get_audit_trail(self, service: OrderService) -> None:
        with pytest.raises(OrderNotFoundError):
            await service.get_audit_trail(UUID(int=404))
