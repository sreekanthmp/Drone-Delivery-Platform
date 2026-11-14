"""Mission planning (Rule 1): turning an order and a drone into a predefined mission.

Pure logic. A mission is a short list of high-level waypoints; the drone's own flight
controller works out the climb, cruise and descent between them. The cloud never plans
or sends anything finer than this, and never changes a mission once it is uploaded.

The plan is a full sortie: takeoff at the drone's home dock, the pickup point, the dropoff
point, back to the dock, and land. Routing is a straight line at a fixed altitude; there is
no airspace, terrain or weather modelling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

from app.domain.geo import haversine_m
from app.domain.models import Drone, Mission, Order, Waypoint, WaypointKind


@dataclass(frozen=True, slots=True)
class MissionPlanParams:
    cruise_altitude_m: float = 60.0
    """Altitude of the return leg."""

    stop_altitude_m: float = 10.0
    """Altitude at the pickup and dropoff points (hover or winch height)."""

    cruise_speed_mps: float = 12.0
    stop_hold_s: float = 45.0
    """Time allowed at each of the pickup and dropoff points."""

    takeoff_landing_s: float = 30.0
    """Allowance for climbing out and descending in."""

    margin_fraction: float = 0.15
    """Padding on the whole estimate."""

    def __post_init__(self) -> None:
        if not math.isfinite(self.cruise_speed_mps) or self.cruise_speed_mps <= 0:
            raise ValueError("cruise_speed_mps must be a positive number")
        for name in (
            "cruise_altitude_m",
            "stop_altitude_m",
            "stop_hold_s",
            "takeoff_landing_s",
            "margin_fraction",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must not be negative")


def plan_mission(
    order: Order,
    drone: Drone,
    *,
    mission_id: UUID,
    params: MissionPlanParams | None = None,
) -> Mission:
    """Plan the sortie for ``order`` flown by ``drone``."""
    params = params or MissionPlanParams()
    home = drone.home
    waypoints = (
        Waypoint(WaypointKind.TAKEOFF, home, 0.0),
        Waypoint(WaypointKind.PICKUP, order.pickup, params.stop_altitude_m),
        Waypoint(WaypointKind.DROPOFF, order.dropoff, params.stop_altitude_m),
        Waypoint(WaypointKind.RETURN, home, params.cruise_altitude_m),
        Waypoint(WaypointKind.LAND, home, 0.0),
    )
    distance_m = (
        haversine_m(home, order.pickup)
        + haversine_m(order.pickup, order.dropoff)
        + haversine_m(order.dropoff, home)
    )
    flying_s = distance_m / params.cruise_speed_mps
    estimate_s = (flying_s + 2 * params.stop_hold_s + params.takeoff_landing_s) * (
        1 + params.margin_fraction
    )
    return Mission(
        mission_id=mission_id,
        waypoints=waypoints,
        cruise_speed_mps=params.cruise_speed_mps,
        estimated_duration_s=estimate_s,
    )
