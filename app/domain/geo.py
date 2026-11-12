"""Small geometry helpers on WGS84 coordinates (pure functions, metres).

Straight-line great-circle distances are all the platform needs for drone selection and
for the mock compliance provider's no-fly zones. There is deliberately no routing,
terrain or airspace modelling here.
"""

from __future__ import annotations

import math
from typing import Final

from app.domain.models.order import Location

EARTH_RADIUS_M: Final = 6_371_008.8


def haversine_m(a: Location, b: Location) -> float:
    """Great-circle distance between two locations, in metres."""
    lat_a, lat_b = math.radians(a.latitude), math.radians(b.latitude)
    d_lat = lat_b - lat_a
    d_lon = math.radians(b.longitude - a.longitude)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _offset_m(origin: Location, other: Location) -> tuple[float, float]:
    """``other`` as (east, north) metres from ``origin`` using a local flat projection."""
    d_lon = (other.longitude - origin.longitude + 180.0) % 360.0 - 180.0  # across the antimeridian
    east = math.radians(d_lon) * math.cos(math.radians(origin.latitude)) * EARTH_RADIUS_M
    north = math.radians(other.latitude - origin.latitude) * EARTH_RADIUS_M
    return east, north


def distance_to_segment_m(point: Location, start: Location, end: Location) -> float:
    """Shortest distance from ``point`` to the straight segment ``start``-``end``, in metres.

    Uses a flat projection centred on ``point``, which is accurate for the few-kilometre
    distances a no-fly zone check involves.
    """
    ax, ay = _offset_m(point, start)
    bx, by = _offset_m(point, end)
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_squared))
    return math.hypot(ax + t * dx, ay + t * dy)
