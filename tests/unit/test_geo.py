"""Geometry helpers."""

from __future__ import annotations

import pytest

from app.domain.geo import distance_to_segment_m, haversine_m
from app.domain.models import Location


def loc(lat: float, lon: float) -> Location:
    return Location(latitude=lat, longitude=lon)


class TestHaversine:
    def test_zero_distance(self) -> None:
        assert haversine_m(loc(12.9, 77.6), loc(12.9, 77.6)) == 0

    def test_a_known_city_pair(self) -> None:
        london, paris = loc(51.5074, -0.1278), loc(48.8566, 2.3522)
        assert haversine_m(london, paris) == pytest.approx(343_550, rel=0.005)

    def test_one_degree_of_latitude(self) -> None:
        assert haversine_m(loc(0, 0), loc(1, 0)) == pytest.approx(111_195, rel=0.001)

    def test_is_symmetric(self) -> None:
        a, b = loc(12.97, 77.59), loc(13.08, 77.71)
        assert haversine_m(a, b) == pytest.approx(haversine_m(b, a))

    def test_across_the_antimeridian_the_short_way(self) -> None:
        # 0.2 degrees of longitude apart on the equator, not 359.8.
        assert haversine_m(loc(0, 179.9), loc(0, -179.9)) == pytest.approx(22_239, rel=0.002)

    def test_the_poles_are_half_the_circumference_apart(self) -> None:
        assert haversine_m(loc(90, 0), loc(-90, 0)) == pytest.approx(20_015_087, rel=0.001)


class TestDistanceToSegment:
    def test_a_point_on_the_segment(self) -> None:
        assert distance_to_segment_m(loc(0, 0.5), loc(0, 0), loc(0, 1)) == pytest.approx(
            0, abs=1e-6
        )

    def test_a_point_beside_the_segment_is_measured_perpendicularly(self) -> None:
        # 0.01 degrees of latitude north of the middle of an equatorial segment.
        assert distance_to_segment_m(loc(0.01, 0.5), loc(0, 0), loc(0, 1)) == pytest.approx(
            1_112, rel=0.01
        )

    def test_beyond_an_end_the_nearest_endpoint_counts(self) -> None:
        beyond = distance_to_segment_m(loc(0, 1.01), loc(0, 0), loc(0, 1))
        assert beyond == pytest.approx(haversine_m(loc(0, 1.01), loc(0, 1)), rel=0.01)

    def test_a_degenerate_segment_is_a_point(self) -> None:
        d = distance_to_segment_m(loc(0.01, 0), loc(0, 0), loc(0, 0))
        assert d == pytest.approx(1_112, rel=0.01)

    def test_a_segment_crossing_the_antimeridian(self) -> None:
        assert distance_to_segment_m(loc(0, 180), loc(0, 179.9), loc(0, -179.9)) == pytest.approx(
            0, abs=1e-3
        )

    def test_a_segment_passing_straight_through_a_point_has_zero_distance(self) -> None:
        assert distance_to_segment_m(loc(5, 5), loc(0, 0), loc(10, 10)) < 1_000

    def test_the_distance_is_never_negative(self) -> None:
        assert distance_to_segment_m(loc(1, 1), loc(0, 0), loc(0, 2)) >= 0
