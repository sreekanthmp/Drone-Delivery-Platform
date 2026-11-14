"""Mission planning: a predefined sortie of a handful of high-level waypoints (Rule 1)."""

from __future__ import annotations

import math
from dataclasses import replace
from uuid import UUID

import pytest

from app.dispatch.mission import MissionPlanParams, plan_mission
from app.domain.geo import haversine_m
from app.domain.models import Mission, WaypointKind
from tests.conftest import DROPOFF, HOME, PICKUP, build_drone

MISSION_ID = UUID(int=0x77)


@pytest.fixture
def mission(make_order: object) -> Mission:
    order = make_order()  # type: ignore[operator]
    return plan_mission(order, build_drone(), mission_id=MISSION_ID)


class TestWaypoints:
    def test_a_full_sortie_from_the_dock_and_back(self, mission: Mission) -> None:
        assert [w.kind for w in mission.waypoints] == [
            WaypointKind.TAKEOFF,
            WaypointKind.PICKUP,
            WaypointKind.DROPOFF,
            WaypointKind.RETURN,
            WaypointKind.LAND,
        ]

    def test_the_locations(self, mission: Mission) -> None:
        assert [w.location for w in mission.waypoints] == [HOME, PICKUP, DROPOFF, HOME, HOME]

    def test_it_is_only_a_few_high_level_waypoints_never_a_steering_path(
        self, mission: Mission
    ) -> None:
        assert len(mission.waypoints) <= 8

    def test_altitudes(self, mission: Mission) -> None:
        params = MissionPlanParams()
        assert [w.altitude_m for w in mission.waypoints] == [
            0.0,
            params.stop_altitude_m,
            params.stop_altitude_m,
            params.cruise_altitude_m,
            0.0,
        ]

    def test_it_uses_the_drones_own_home_dock(self, make_order: object) -> None:
        elsewhere = HOME.__class__(latitude=12.95, longitude=77.65)
        order = make_order()  # type: ignore[operator]

        planned = plan_mission(order, build_drone(home=elsewhere), mission_id=MISSION_ID)

        assert planned.waypoints[0].location == elsewhere
        assert planned.waypoints[-1].location == elsewhere

    def test_the_mission_id_is_the_one_given(self, mission: Mission) -> None:
        assert mission.mission_id == MISSION_ID


class TestDuration:
    def test_the_estimate_follows_the_documented_formula(self, mission: Mission) -> None:
        params = MissionPlanParams()
        distance = (
            haversine_m(HOME, PICKUP) + haversine_m(PICKUP, DROPOFF) + haversine_m(DROPOFF, HOME)
        )
        expected = (
            distance / params.cruise_speed_mps + 2 * params.stop_hold_s + params.takeoff_landing_s
        ) * (1 + params.margin_fraction)

        assert mission.estimated_duration_s == pytest.approx(expected)
        assert mission.cruise_speed_mps == params.cruise_speed_mps

    def test_a_typical_sortie_outlasts_the_default_mock_permission(self, mission: Mission) -> None:
        """Why the default permission validity is 60 minutes, not 15."""
        assert mission.estimated_duration_s > 15 * 60
        assert mission.estimated_duration_s < 60 * 60

    def test_a_longer_hold_lengthens_the_estimate(self, make_order: object) -> None:
        order = make_order()  # type: ignore[operator]
        base = plan_mission(order, build_drone(), mission_id=MISSION_ID)

        longer = plan_mission(
            order,
            build_drone(),
            mission_id=MISSION_ID,
            params=MissionPlanParams(stop_hold_s=120.0),
        )

        assert longer.estimated_duration_s == pytest.approx(
            base.estimated_duration_s + 2 * (120.0 - 45.0) * 1.15
        )

    def test_a_faster_drone_flies_shorter(self, make_order: object) -> None:
        order = make_order()  # type: ignore[operator]
        slow = plan_mission(order, build_drone(), mission_id=MISSION_ID)
        fast = plan_mission(
            order,
            build_drone(),
            mission_id=MISSION_ID,
            params=MissionPlanParams(cruise_speed_mps=24.0),
        )

        assert fast.estimated_duration_s < slow.estimated_duration_s


class TestDeterminism:
    def test_the_same_inputs_give_the_same_mission_and_checksum(self, make_order: object) -> None:
        order = make_order()  # type: ignore[operator]

        a = plan_mission(order, build_drone(), mission_id=MISSION_ID)
        b = plan_mission(order, build_drone(), mission_id=MISSION_ID)

        assert a == b
        assert a.checksum == b.checksum

    def test_a_different_mission_id_changes_the_checksum(self, make_order: object) -> None:
        order = make_order()  # type: ignore[operator]

        a = plan_mission(order, build_drone(), mission_id=MISSION_ID)
        b = plan_mission(order, build_drone(), mission_id=UUID(int=0x78))

        assert a.checksum != b.checksum


class TestParams:
    def test_defaults_are_valid(self) -> None:
        MissionPlanParams()

    @pytest.mark.parametrize("speed", [0.0, -1.0, math.nan, math.inf])
    def test_speed_must_be_a_positive_finite_number(self, speed: float) -> None:
        with pytest.raises(ValueError, match="cruise_speed_mps"):
            MissionPlanParams(cruise_speed_mps=speed)

    @pytest.mark.parametrize(
        "field",
        [
            "cruise_altitude_m",
            "stop_altitude_m",
            "stop_hold_s",
            "takeoff_landing_s",
            "margin_fraction",
        ],
    )
    @pytest.mark.parametrize("value", [-0.1, math.nan, math.inf])
    def test_other_values_must_be_finite_and_not_negative(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            replace(MissionPlanParams(), **{field: value})

    def test_zero_altitudes_and_holds_are_allowed(self) -> None:
        MissionPlanParams(
            cruise_altitude_m=0.0, stop_altitude_m=0.0, stop_hold_s=0.0, margin_fraction=0.0
        )
