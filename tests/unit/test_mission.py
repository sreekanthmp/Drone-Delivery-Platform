"""Missions: validation, canonical form and checksum."""

from __future__ import annotations

import math
import re
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import MissionValidationError
from app.domain.geo import haversine_m
from app.domain.models import Location, Mission, Waypoint, WaypointKind
from tests.conftest import DROPOFF, HOME, PICKUP, build_mission

K = WaypointKind


def waypoints(*kinds: WaypointKind) -> tuple[Waypoint, ...]:
    return tuple(Waypoint(kind, HOME, 50.0) for kind in kinds)


class TestValidation:
    def test_a_valid_mission(self) -> None:
        mission = build_mission()
        assert len(mission.waypoints) == 5
        assert mission.waypoints[0].kind is K.TAKEOFF
        assert mission.waypoints[-1].kind is K.LAND

    def test_waypoints_given_as_a_list_become_a_tuple(self) -> None:
        mission = Mission(
            mission_id=UUID(int=1),
            waypoints=list(waypoints(K.TAKEOFF, K.LAND)),  # type: ignore[arg-type]
            cruise_speed_mps=10,
            estimated_duration_s=60,
        )
        assert isinstance(mission.waypoints, tuple)

    def test_a_minimal_mission_is_a_takeoff_and_a_landing(self) -> None:
        assert Mission(UUID(int=1), waypoints(K.TAKEOFF, K.LAND), 10, 60)

    @pytest.mark.parametrize(
        ("kinds", "message"),
        [
            ((), "at least"),
            ((K.TAKEOFF,), "at least"),
            ((K.PICKUP, K.LAND), "start with a takeoff"),
            ((K.TAKEOFF, K.DROPOFF), "end with a landing"),
            ((K.LAND, K.TAKEOFF), "start with a takeoff"),
        ],
    )
    def test_the_shape_is_enforced(self, kinds: tuple[WaypointKind, ...], message: str) -> None:
        with pytest.raises(MissionValidationError, match=message):
            Mission(UUID(int=1), waypoints(*kinds), 10, 60)

    @pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
    def test_speed_and_duration_must_be_positive_numbers(self, value: float) -> None:
        with pytest.raises(MissionValidationError, match="cruise_speed_mps"):
            build_mission(cruise_speed_mps=value)
        with pytest.raises(MissionValidationError, match="estimated_duration_s"):
            build_mission(estimated_duration_s=value)

    @pytest.mark.parametrize("altitude", [-1, math.nan, math.inf])
    def test_waypoint_altitude_must_be_a_non_negative_number(self, altitude: float) -> None:
        with pytest.raises(MissionValidationError, match="altitude"):
            Waypoint(K.CRUISE, HOME, altitude)

    def test_is_immutable(self) -> None:
        with pytest.raises(FrozenInstanceError):
            build_mission().cruise_speed_mps = 1  # type: ignore[misc]


class TestGeometry:
    def test_route_is_the_waypoint_locations(self) -> None:
        assert build_mission().route == (HOME, PICKUP, DROPOFF, HOME, HOME)

    def test_distance_is_the_sum_of_the_legs(self) -> None:
        expected = (
            haversine_m(HOME, PICKUP)
            + haversine_m(PICKUP, DROPOFF)
            + haversine_m(DROPOFF, HOME)
            + haversine_m(HOME, HOME)
        )
        assert build_mission().distance_m == pytest.approx(expected)


class TestChecksum:
    def test_is_a_sha256_hex_digest(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{64}", build_mission().checksum)

    def test_the_same_content_gives_the_same_checksum(self) -> None:
        assert build_mission().checksum == build_mission().checksum
        assert build_mission() is not build_mission()

    @pytest.mark.parametrize(
        "change",
        [
            {"mission_id": UUID(int=0x99)},
            {"cruise_speed_mps": 13.0},
            {"estimated_duration_s": 601.0},
        ],
    )
    def test_every_scalar_field_affects_the_checksum(self, change: dict[str, Any]) -> None:
        assert build_mission(**change).checksum != build_mission().checksum

    def test_waypoint_content_and_order_affect_the_checksum(self) -> None:
        base = build_mission()
        higher = list(base.waypoints)
        higher[1] = Waypoint(K.PICKUP, PICKUP, 61.0)
        moved = list(base.waypoints)
        moved[2] = Waypoint(K.DROPOFF, Location(DROPOFF.latitude + 1e-6, DROPOFF.longitude), 60.0)
        swapped = list(base.waypoints)
        swapped[1], swapped[2] = swapped[2], swapped[1]

        checksums = {
            base.checksum,
            build_mission(waypoints=tuple(higher)).checksum,
            build_mission(waypoints=tuple(moved)).checksum,
            build_mission(waypoints=tuple(swapped)).checksum,
        }
        assert len(checksums) == 4

    def test_survives_a_dict_round_trip(self) -> None:
        mission = build_mission()
        assert Mission.from_dict(mission.as_dict()) == mission
        assert Mission.from_dict(mission.as_dict()).checksum == mission.checksum

    def test_survives_a_json_round_trip(self) -> None:
        import json

        mission = build_mission()
        again = Mission.from_dict(json.loads(json.dumps(mission.as_dict())))
        assert again.checksum == mission.checksum


class TestFromDict:
    def test_as_dict_is_json_compatible(self) -> None:
        import json

        json.dumps(build_mission().as_dict(), allow_nan=False)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.pop("waypoints"),
            lambda d: d.pop("mission_id"),
            lambda d: d.pop("cruise_speed_mps"),
            lambda d: d.update(waypoints="not a list"),
            lambda d: d.update(waypoints=None),
            lambda d: d.update(mission_id="not-a-uuid"),
            lambda d: d["waypoints"][0].update(kind="teleport"),
            lambda d: d["waypoints"][0].pop("latitude"),
            lambda d: d["waypoints"][0].update(latitude=999),
            lambda d: d["waypoints"][0].update(altitude_m=-5),
            lambda d: d["waypoints"].pop(0),
            lambda d: d.update(cruise_speed_mps=0),
        ],
    )
    def test_malformed_data_raises_only_a_mission_validation_error(self, mutate: Any) -> None:
        data = build_mission().as_dict()
        mutate(data)
        with pytest.raises(MissionValidationError):
            Mission.from_dict(data)
