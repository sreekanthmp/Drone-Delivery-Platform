"""Choosing a drone for an order (Rule 2, step 1: identify a suitable drone).

Pure logic: no I/O, no clock. Given the order and the candidate drones it either picks
one or explains, per drone, why it was ruled out.

A drone is suitable when it is available, strong enough for the package, has enough
battery, and can fly the whole sortie (home -> pickup -> dropoff -> home) within its range
less a safety reserve, starting from a home dock that is not too far from the pickup.
Among suitable drones the nearest to the pickup wins; ties go to the higher battery, then
to the lowest id so the choice is deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.geo import haversine_m
from app.domain.models import Drone, DroneStatus, Order


class Rejection(StrEnum):
    NOT_AVAILABLE = "not_available"
    PAYLOAD_TOO_HEAVY = "payload_too_heavy"
    BATTERY_LOW = "battery_low"
    OUT_OF_RANGE = "out_of_range"
    TOO_FAR_FROM_PICKUP = "too_far_from_pickup"


@dataclass(frozen=True, slots=True)
class AssignmentPolicy:
    min_battery_percent: float = 30.0
    range_reserve_fraction: float = 0.2
    """Share of a drone's range kept back as a safety margin (0.2 = plan for 80%)."""

    max_pickup_distance_km: float = 10.0
    """How far a drone's home dock may be from the pickup point."""

    def __post_init__(self) -> None:
        if not 0 <= self.min_battery_percent <= 100:
            raise ValueError("min_battery_percent must be within [0, 100]")
        if not 0 <= self.range_reserve_fraction < 1:
            raise ValueError("range_reserve_fraction must be within [0, 1)")
        if not math.isfinite(self.max_pickup_distance_km) or self.max_pickup_distance_km <= 0:
            raise ValueError("max_pickup_distance_km must be positive")


@dataclass(frozen=True, slots=True)
class DroneChoice:
    drone: Drone
    pickup_distance_m: float
    """From the drone's home dock to the pickup point."""

    sortie_distance_m: float
    """The whole trip: home -> pickup -> dropoff -> home."""


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    choice: DroneChoice | None
    rejections: Mapping[UUID, tuple[Rejection, ...]]
    """Why each ruled-out drone was ruled out (every reason that applies)."""


def select_drone(
    order: Order,
    drones: Sequence[Drone],
    policy: AssignmentPolicy | None = None,
) -> AssignmentResult:
    """Pick the best suitable drone for ``order``, or report why none is."""
    policy = policy or AssignmentPolicy()
    delivery_leg_m = haversine_m(order.pickup, order.dropoff)

    suitable: list[DroneChoice] = []
    rejections: dict[UUID, tuple[Rejection, ...]] = {}

    for drone in drones:
        if drone.status is not DroneStatus.AVAILABLE:
            rejections[drone.id] = (Rejection.NOT_AVAILABLE,)
            continue

        pickup_m = haversine_m(drone.home, order.pickup)
        sortie_m = pickup_m + delivery_leg_m + haversine_m(order.dropoff, drone.home)
        problems: list[Rejection] = []
        if order.package.weight_kg > drone.max_payload_kg:
            problems.append(Rejection.PAYLOAD_TOO_HEAVY)
        if drone.battery_percent < policy.min_battery_percent:
            problems.append(Rejection.BATTERY_LOW)
        usable_range_m = drone.max_range_km * 1000 * (1 - policy.range_reserve_fraction)
        if sortie_m > usable_range_m:
            problems.append(Rejection.OUT_OF_RANGE)
        if pickup_m > policy.max_pickup_distance_km * 1000:
            problems.append(Rejection.TOO_FAR_FROM_PICKUP)

        if problems:
            rejections[drone.id] = tuple(problems)
        else:
            suitable.append(DroneChoice(drone, pickup_m, sortie_m))

    suitable.sort(key=lambda c: (c.pickup_distance_m, -c.drone.battery_percent, str(c.drone.id)))
    return AssignmentResult(choice=suitable[0] if suitable else None, rejections=rejections)
