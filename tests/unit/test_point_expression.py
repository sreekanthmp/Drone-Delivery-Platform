"""The SQL point builder must put longitude first (PostGIS takes x = longitude, y = latitude)."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.db.spatial import point
from app.domain.models import Location


def sql(location: Location) -> str:
    return str(
        point(location).compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


def test_longitude_comes_before_latitude() -> None:
    # Bengaluru: lat 12.9716, lon 77.5946. A swap would read ST_MakePoint(12.9716, 77.5946),
    # which is still a valid coordinate pair, just in the wrong place (near Svalbard), so
    # nothing would fail loudly; only an explicit test catches it.
    assert "ST_MakePoint(77.5946, 12.9716)" in sql(Location(latitude=12.9716, longitude=77.5946))


def test_negative_and_boundary_values_keep_their_order() -> None:
    assert "ST_MakePoint(-180.0, -90.0)" in sql(Location(latitude=-90.0, longitude=-180.0))


def test_the_point_uses_wgs84_and_is_cast_to_geography() -> None:
    expression = sql(Location(latitude=1.0, longitude=2.0))
    assert "ST_SetSRID" in expression
    assert "4326" in expression
    assert "geography" in expression.lower()
