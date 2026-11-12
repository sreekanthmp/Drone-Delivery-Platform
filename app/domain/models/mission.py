"""Missions: predefined flights that are uploaded to a drone (Rule 1).

A mission is *data*. The cloud plans it, uploads it and may later send the high-level
commands start / pause / return-to-home / land. The drone's own flight controller flies
it. Nothing here (or anywhere in the platform) steers a drone in real time.

The checksum is a SHA-256 over a canonical encoding of the mission. The drone echoes it
back when it accepts an upload, so the platform can prove the drone holds exactly the
mission that was planned.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any, Self
from uuid import UUID

from app.domain.exceptions import MissionValidationError, OrderValidationError
from app.domain.geo import haversine_m
from app.domain.models.order import Location


class WaypointKind(StrEnum):
    TAKEOFF = "takeoff"
    CRUISE = "cruise"
    PICKUP = "pickup"
    DROPOFF = "dropoff"
    RETURN = "return"
    LAND = "land"


@dataclass(frozen=True, slots=True)
class Waypoint:
    kind: WaypointKind
    location: Location
    altitude_m: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.altitude_m) or self.altitude_m < 0:
            raise MissionValidationError(
                f"waypoint altitude must be a non-negative number of metres, got {self.altitude_m}"
            )


@dataclass(frozen=True, slots=True)
class Mission:
    mission_id: UUID
    waypoints: tuple[Waypoint, ...]
    cruise_speed_mps: float
    estimated_duration_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "waypoints", tuple(self.waypoints))
        if len(self.waypoints) < 2:
            raise MissionValidationError("a mission needs at least a takeoff and a landing")
        if self.waypoints[0].kind is not WaypointKind.TAKEOFF:
            raise MissionValidationError("a mission must start with a takeoff waypoint")
        if self.waypoints[-1].kind is not WaypointKind.LAND:
            raise MissionValidationError("a mission must end with a landing waypoint")
        if not math.isfinite(self.cruise_speed_mps) or self.cruise_speed_mps <= 0:
            raise MissionValidationError("cruise_speed_mps must be a positive number")
        if not math.isfinite(self.estimated_duration_s) or self.estimated_duration_s <= 0:
            raise MissionValidationError("estimated_duration_s must be a positive number")

    @property
    def route(self) -> tuple[Location, ...]:
        return tuple(waypoint.location for waypoint in self.waypoints)

    @property
    def distance_m(self) -> float:
        route = self.route
        return sum(haversine_m(a, b) for a, b in pairwise(route))

    def as_dict(self) -> dict[str, Any]:
        """Canonical JSON-compatible form (used for storage, upload and the checksum)."""
        return {
            "mission_id": str(self.mission_id),
            "cruise_speed_mps": self.cruise_speed_mps,
            "estimated_duration_s": self.estimated_duration_s,
            "waypoints": [
                {
                    "kind": waypoint.kind.value,
                    "latitude": waypoint.location.latitude,
                    "longitude": waypoint.location.longitude,
                    "altitude_m": waypoint.altitude_m,
                }
                for waypoint in self.waypoints
            ],
        }

    @property
    def checksum(self) -> str:
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a mission from :meth:`as_dict` output. Anything malformed is rejected."""
        try:
            waypoints = [
                Waypoint(
                    kind=WaypointKind(item["kind"]),
                    location=Location(latitude=item["latitude"], longitude=item["longitude"]),
                    altitude_m=item["altitude_m"],
                )
                for item in _as_sequence(data["waypoints"])
            ]
            return cls(
                mission_id=UUID(data["mission_id"]),
                waypoints=tuple(waypoints),
                cruise_speed_mps=data["cruise_speed_mps"],
                estimated_duration_s=data["estimated_duration_s"],
            )
        except MissionValidationError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OrderValidationError) as error:
            raise MissionValidationError(f"malformed mission data: {error!r}") from error


def _as_sequence(value: object) -> Sequence[Mapping[str, Any]]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("waypoints must be a list")
    return value
