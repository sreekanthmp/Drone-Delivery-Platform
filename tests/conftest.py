"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.flight_state_machine import FlightStateMachine
from app.domain.models import (
    DenialCode,
    Drone,
    DroneStatus,
    Flight,
    FlightPermission,
    FlightStatus,
    Location,
    Mission,
    Order,
    OrderStatus,
    Package,
    PermissionDenial,
    Waypoint,
    WaypointKind,
)
from app.domain.order_state_machine import OrderStateMachine

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
DRONE_ID = UUID(int=0xD0)
FLIGHT_ID = UUID(int=0xF1)
ORDER_ID = UUID(int=0xA1)
HOME = Location(latitude=12.9800, longitude=77.6000)
PICKUP = Location(latitude=12.9716, longitude=77.5946)
DROPOFF = Location(latitude=12.9352, longitude=77.6245)
PERMISSION_VALIDITY = timedelta(minutes=15)

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


# --- drones, missions, permissions and flights -------------------------------------------------


def build_drone(**overrides: Any) -> Drone:
    fields: dict[str, Any] = {
        "id": DRONE_ID,
        "name": "drone-1",
        "status": DroneStatus.AVAILABLE,
        "home": HOME,
        "max_payload_kg": 5.0,
        "max_range_km": 20.0,
        "battery_percent": 90.0,
        "created_at": T0,
        "updated_at": T0,
    }
    fields.update(overrides)
    return Drone.rehydrate(**fields)


def build_mission(**overrides: Any) -> Mission:
    """A full sortie: home -> pickup -> dropoff -> home (10 minutes)."""
    fields: dict[str, Any] = {
        "mission_id": UUID(int=0x51),
        "waypoints": (
            Waypoint(WaypointKind.TAKEOFF, HOME, 0.0),
            Waypoint(WaypointKind.PICKUP, PICKUP, 60.0),
            Waypoint(WaypointKind.DROPOFF, DROPOFF, 60.0),
            Waypoint(WaypointKind.RETURN, HOME, 60.0),
            Waypoint(WaypointKind.LAND, HOME, 0.0),
        ),
        "cruise_speed_mps": 12.0,
        "estimated_duration_s": 600.0,
    }
    fields.update(overrides)
    return Mission(**fields)


def build_permission(**overrides: Any) -> FlightPermission:
    fields: dict[str, Any] = {
        "permission_id": "PERM-1",
        "issuer": "test-authority",
        "flight_id": FLIGHT_ID,
        "drone_id": DRONE_ID,
        "issued_at": T0,
        "valid_from": T0,
        "valid_until": T0 + PERMISSION_VALIDITY,
    }
    fields.update(overrides)
    return FlightPermission(**fields)


_S = FlightStatus
_PERMISSION_STATES = {
    _S.PERMISSION_GRANTED,
    _S.MISSION_UPLOADED,
    _S.UPLOAD_FAILED,
    _S.STARTED,
    _S.PAUSED,
    _S.COMPLETED,
    _S.ABORTED,
}
_ACK_STATES = {_S.MISSION_UPLOADED, _S.STARTED, _S.PAUSED, _S.COMPLETED, _S.ABORTED}
_STARTED_STATES = {_S.STARTED, _S.PAUSED, _S.COMPLETED, _S.ABORTED}
_REASON_STATES = {_S.UPLOAD_FAILED, _S.CANCELLED, _S.ABORTED}


def build_flight(status: FlightStatus = FlightStatus.PLANNED, **overrides: Any) -> Flight:
    """A consistent flight in any status, built the way a repository would (``rehydrate``)."""
    mission = overrides.pop("mission", None) or build_mission()
    flight_id = overrides.get("id", FLIGHT_ID)
    drone_id = overrides.get("drone_id", DRONE_ID)
    fields: dict[str, Any] = {
        "id": flight_id,
        "order_id": ORDER_ID,
        "drone_id": drone_id,
        "status": status,
        "mission": mission,
        "created_at": T0,
        "updated_at": T0,
        "permission": (
            build_permission(flight_id=flight_id, drone_id=drone_id)
            if status in _PERMISSION_STATES
            else None
        ),
        "denial": (
            PermissionDenial(DenialCode.NO_FLY_ZONE, "route enters a no-fly zone")
            if status is _S.PERMISSION_DENIED
            else None
        ),
        "acknowledged_checksum": mission.checksum if status in _ACK_STATES else None,
        "failure_reason": "test reason" if status in _REASON_STATES else None,
        "started_at": T0 if status in _STARTED_STATES else None,
    }
    fields.update(overrides)
    return Flight.rehydrate(**fields)


@pytest.fixture
def make_drone() -> Callable[..., Drone]:
    return build_drone


@pytest.fixture
def make_mission() -> Callable[..., Mission]:
    return build_mission


@pytest.fixture
def make_permission() -> Callable[..., FlightPermission]:
    return build_permission


@pytest.fixture
def make_flight() -> Callable[..., Flight]:
    return build_flight


@pytest.fixture
def flight_machine(clock: FakeClock, ids: SequentialIds) -> FlightStateMachine:
    return FlightStateMachine(clock=clock, id_factory=ids)
