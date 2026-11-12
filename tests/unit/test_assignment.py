"""Drone selection (Rule 2, step 1)."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from app.dispatch.assignment import (
    AssignmentPolicy,
    Rejection,
    select_drone,
)
from app.domain.geo import haversine_m
from app.domain.models import Drone, DroneStatus, Location, Order, OrderStatus
from tests.conftest import build_drone

R = Rejection
MakeOrder = Callable[..., Order]

# One degree of latitude is ~111.2 km, so these home docks sit at roughly these distances north
# of the order's pickup point (12.9716, 77.5946).
NEAR = 0.001  # ~0.1 km
MID = 0.02  # ~2.2 km
EDGE = 0.08  # ~8.9 km
FAR = 0.2  # ~22 km


def home_north_of_pickup(pickup: Location, degrees: float) -> Location:
    return Location(latitude=pickup.latitude + degrees, longitude=pickup.longitude)


@pytest.fixture
def order(make_order: MakeOrder) -> Order:
    return make_order(OrderStatus.CREATED)


def drone_at(order: Order, degrees: float, n: int, **overrides: Any) -> Drone:
    fields: dict[str, Any] = {
        "id": UUID(int=n),
        "name": f"drone-{n}",
        "home": home_north_of_pickup(order.pickup, degrees),
        "max_range_km": 200.0,  # range is not the constraint unless a test says so
    }
    fields.update(overrides)
    return build_drone(**fields)


class TestPolicyValidation:
    def test_defaults(self) -> None:
        policy = AssignmentPolicy()
        assert (policy.min_battery_percent, policy.range_reserve_fraction) == (30.0, 0.2)
        assert policy.max_pickup_distance_km == 10.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_battery_percent": -1},
            {"min_battery_percent": 101},
            {"range_reserve_fraction": -0.1},
            {"range_reserve_fraction": 1.0},
            {"max_pickup_distance_km": 0},
            {"max_pickup_distance_km": float("nan")},
        ],
    )
    def test_nonsense_is_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="must be"):
            AssignmentPolicy(**kwargs)


class TestChoosing:
    def test_picks_the_drone_nearest_to_the_pickup(self, order: Order) -> None:
        drones = [drone_at(order, MID, 1), drone_at(order, NEAR, 2), drone_at(order, EDGE, 3)]

        result = select_drone(order, drones)

        assert result.choice is not None
        assert result.choice.drone.id == UUID(int=2)
        assert result.rejections == {}

    def test_the_choice_carries_the_distances(self, order: Order) -> None:
        drone = drone_at(order, MID, 1)

        choice = select_drone(order, [drone]).choice

        assert choice is not None
        pickup_m = haversine_m(drone.home, order.pickup)
        assert choice.pickup_distance_m == pytest.approx(pickup_m)
        assert choice.sortie_distance_m == pytest.approx(
            pickup_m
            + haversine_m(order.pickup, order.dropoff)
            + haversine_m(order.dropoff, drone.home)
        )

    def test_ties_go_to_the_higher_battery(self, order: Order) -> None:
        low = drone_at(order, MID, 1, battery_percent=60)
        high = drone_at(order, MID, 2, battery_percent=95)

        result = select_drone(order, [low, high])

        assert result.choice is not None
        assert result.choice.drone.id == high.id

    def test_remaining_ties_go_to_the_lowest_id(self, order: Order) -> None:
        drones = [drone_at(order, MID, n, battery_percent=80) for n in (7, 3, 9)]

        result = select_drone(order, drones)

        assert result.choice is not None
        assert result.choice.drone.id == UUID(int=3)

    def test_the_input_order_never_changes_the_outcome(self, order: Order) -> None:
        drones = [
            drone_at(order, MID, 1, battery_percent=70),
            drone_at(order, MID, 2, battery_percent=70),
            drone_at(order, NEAR, 3),
            drone_at(order, EDGE, 4),
            drone_at(order, FAR, 5),
            drone_at(order, NEAR, 6, status=DroneStatus.OFFLINE),
        ]
        expected = select_drone(order, drones)
        rng = random.Random(7)

        for _ in range(20):
            shuffled = drones[:]
            rng.shuffle(shuffled)
            again = select_drone(order, shuffled)
            assert again.choice == expected.choice
            assert again.rejections == expected.rejections

    def test_no_drones_means_no_choice(self, order: Order) -> None:
        result = select_drone(order, [])
        assert result.choice is None
        assert result.rejections == {}

    def test_inputs_are_not_modified(self, order: Order) -> None:
        drones = [drone_at(order, MID, 1), drone_at(order, NEAR, 2)]
        snapshot = list(drones)

        select_drone(order, drones)

        assert drones == snapshot


class TestAvailability:
    @pytest.mark.parametrize(
        "status",
        [
            DroneStatus.ASSIGNED,
            DroneStatus.ON_MISSION,
            DroneStatus.RETURNING,
            DroneStatus.MAINTENANCE,
            DroneStatus.OFFLINE,
        ],
    )
    def test_only_available_drones_are_considered(self, order: Order, status: DroneStatus) -> None:
        drone = drone_at(order, NEAR, 1, status=status)

        result = select_drone(order, [drone])

        assert result.choice is None
        assert result.rejections == {drone.id: (R.NOT_AVAILABLE,)}

    def test_an_unavailable_drone_is_not_blamed_for_anything_else(self, order: Order) -> None:
        drone = drone_at(
            order, FAR, 1, status=DroneStatus.OFFLINE, battery_percent=1, max_payload_kg=0.1
        )

        assert select_drone(order, [drone]).rejections == {drone.id: (R.NOT_AVAILABLE,)}

    def test_an_available_drone_is_chosen_over_a_nearer_unavailable_one(self, order: Order) -> None:
        busy = drone_at(order, NEAR, 1, status=DroneStatus.ON_MISSION)
        free = drone_at(order, EDGE, 2)

        result = select_drone(order, [busy, free])

        assert result.choice is not None
        assert result.choice.drone.id == free.id


class TestPayload:
    def test_a_package_heavier_than_the_drone_can_carry(self, order: Order) -> None:
        weak = drone_at(order, NEAR, 1, max_payload_kg=order.package.weight_kg - 0.01)

        result = select_drone(order, [weak])

        assert result.choice is None
        assert result.rejections[weak.id] == (R.PAYLOAD_TOO_HEAVY,)

    def test_a_package_exactly_at_the_limit_is_fine(self, order: Order) -> None:
        exact = drone_at(order, NEAR, 1, max_payload_kg=order.package.weight_kg)
        assert select_drone(order, [exact]).choice is not None


class TestBattery:
    def test_below_the_minimum_is_rejected(self, order: Order) -> None:
        low = drone_at(order, NEAR, 1, battery_percent=29.9)
        assert select_drone(order, [low]).rejections[low.id] == (R.BATTERY_LOW,)

    def test_exactly_the_minimum_is_fine(self, order: Order) -> None:
        assert select_drone(order, [drone_at(order, NEAR, 1, battery_percent=30.0)]).choice

    def test_the_minimum_is_configurable(self, order: Order) -> None:
        drone = drone_at(order, NEAR, 1, battery_percent=60)

        assert select_drone(order, [drone]).choice is not None
        strict = select_drone(order, [drone], AssignmentPolicy(min_battery_percent=70))
        assert strict.rejections[drone.id] == (R.BATTERY_LOW,)


class TestRange:
    def sortie_m(self, order: Order, home: Location) -> float:
        return (
            haversine_m(home, order.pickup)
            + haversine_m(order.pickup, order.dropoff)
            + haversine_m(order.dropoff, home)
        )

    def range_for(self, order: Order, degrees: float, factor: float, reserve: float = 0.2) -> float:
        home = home_north_of_pickup(order.pickup, degrees)
        return self.sortie_m(order, home) / 1000 / (1 - reserve) * factor

    def test_a_drone_that_cannot_fly_the_whole_sortie_is_rejected(self, order: Order) -> None:
        drone = drone_at(order, MID, 1, max_range_km=self.range_for(order, MID, 0.999))

        assert select_drone(order, [drone]).rejections[drone.id] == (R.OUT_OF_RANGE,)

    def test_a_drone_that_just_can_is_accepted(self, order: Order) -> None:
        drone = drone_at(order, MID, 1, max_range_km=self.range_for(order, MID, 1.001))

        assert select_drone(order, [drone]).choice is not None

    def test_the_return_leg_counts(self, order: Order) -> None:
        """A drone whose home is far on the other side of the dropoff needs more range."""
        # range suits home->pickup->dropoff but not dropoff->home when home is far past it
        far_home = Location(order.dropoff.latitude - 0.2, order.dropoff.longitude)
        drone = drone_at(order, 0, 1, home=far_home, max_range_km=30.0)

        result = select_drone(order, [drone], AssignmentPolicy(max_pickup_distance_km=100))

        assert R.OUT_OF_RANGE in result.rejections[drone.id]

    def test_the_reserve_is_configurable(self, order: Order) -> None:
        drone = drone_at(order, MID, 1, max_range_km=self.range_for(order, MID, 1.001))

        assert select_drone(order, [drone]).choice is not None
        cautious = select_drone(order, [drone], AssignmentPolicy(range_reserve_fraction=0.5))
        assert cautious.rejections[drone.id] == (R.OUT_OF_RANGE,)


class TestPickupDistance:
    def test_a_dock_too_far_from_the_pickup_is_rejected(self, order: Order) -> None:
        far = drone_at(order, FAR, 1)  # ~22 km

        result = select_drone(order, [far])

        assert result.choice is None
        assert result.rejections[far.id] == (R.TOO_FAR_FROM_PICKUP,)

    def test_a_dock_inside_the_limit_is_fine(self, order: Order) -> None:
        assert select_drone(order, [drone_at(order, EDGE, 1)]).choice is not None

    def test_the_limit_is_configurable(self, order: Order) -> None:
        drone = drone_at(order, MID, 1)  # ~2.2 km

        tight = select_drone(order, [drone], AssignmentPolicy(max_pickup_distance_km=1))
        assert tight.rejections[drone.id] == (R.TOO_FAR_FROM_PICKUP,)


class TestSeveralReasons:
    def test_every_applicable_reason_is_reported(self, order: Order) -> None:
        bad = drone_at(order, FAR, 1, max_payload_kg=0.1, battery_percent=5, max_range_km=1.0)

        reasons = select_drone(order, [bad]).rejections[bad.id]

        assert set(reasons) == {
            R.PAYLOAD_TOO_HEAVY,
            R.BATTERY_LOW,
            R.OUT_OF_RANGE,
            R.TOO_FAR_FROM_PICKUP,
        }

    def test_each_ruled_out_drone_is_explained_and_the_winner_is_not(self, order: Order) -> None:
        winner = drone_at(order, MID, 1)
        flat = drone_at(order, NEAR, 2, battery_percent=10)
        busy = drone_at(order, NEAR, 3, status=DroneStatus.ASSIGNED)

        result = select_drone(order, [winner, flat, busy])

        assert result.choice is not None
        assert result.choice.drone.id == winner.id
        assert set(result.rejections) == {flat.id, busy.id}
        assert result.rejections[flat.id] == (R.BATTERY_LOW,)
        assert result.rejections[busy.id] == (R.NOT_AVAILABLE,)

    def test_no_choice_when_every_drone_is_ruled_out(self, order: Order) -> None:
        drones = [
            drone_at(order, NEAR, 1, status=DroneStatus.OFFLINE),
            drone_at(order, NEAR, 2, battery_percent=1),
            drone_at(order, FAR, 3),
        ]

        result = select_drone(order, drones)

        assert result.choice is None
        assert set(result.rejections) == {d.id for d in drones}
