"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.models import Location, Order, OrderStatus, Package
from app.domain.order_state_machine import OrderStateMachine

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
DRONE_ID = UUID(int=0xD0)

# Statuses that only make sense for an order that already has a drone.
STATUSES_WITH_DRONE = frozenset(
    {
        OrderStatus.ASSIGNED,
        OrderStatus.PICKED_UP,
        OrderStatus.IN_FLIGHT,
        OrderStatus.ARRIVING,
        OrderStatus.DELIVERED,
        OrderStatus.RETURNED,
    }
)

_UNSET: Any = object()


class FakeClock:
    """A controllable clock that also counts how often it was read."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class SequentialIds:
    """Deterministic UUIDs: 1, 2, 3, ... Counts how many were issued."""

    def __init__(self) -> None:
        self.issued = 0

    def __call__(self) -> UUID:
        self.issued += 1
        return UUID(int=self.issued)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ids() -> SequentialIds:
    return SequentialIds()


@pytest.fixture
def machine(clock: FakeClock, ids: SequentialIds) -> OrderStateMachine:
    return OrderStateMachine(clock=clock, id_factory=ids)


@pytest.fixture
def pickup() -> Location:
    return Location(latitude=12.9716, longitude=77.5946)


@pytest.fixture
def dropoff() -> Location:
    return Location(latitude=12.9352, longitude=77.6245)


@pytest.fixture
def package() -> Package:
    return Package(weight_kg=1.5, description="2 meals")


@pytest.fixture
def make_order(pickup: Location, dropoff: Location, package: Package) -> Callable[..., Order]:
    """Build an order in any status via ``Order.rehydrate``, as a repository would."""

    def _make(
        status: OrderStatus = OrderStatus.CREATED, *, drone_id: Any = _UNSET, **kw: Any
    ) -> Order:
        if drone_id is _UNSET:
            drone_id = DRONE_ID if status in STATUSES_WITH_DRONE else None
        fields: dict[str, Any] = {
            "id": UUID(int=0xA1),
            "client_id": "mock",
            "external_order_id": "ORD-1",
            "pickup": pickup,
            "dropoff": dropoff,
            "package": package,
            "status": status,
            "created_at": T0,
            "updated_at": T0,
            "drone_id": drone_id,
        }
        fields.update(kw)
        return Order.rehydrate(**fields)

    return _make
