"""PostGIS expression helpers shared by the repositories.

PostGIS orders coordinates as (longitude, latitude) = (x, y): the opposite of the domain's
``Location(latitude, longitude)``. Keeping the conversion in one place, covered by tests,
keeps a swap from ever going unnoticed.
"""

from __future__ import annotations

from typing import Any

from geoalchemy2 import Geography, Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import ColumnElement, func
from sqlalchemy import cast as sql_cast

from app.domain.models import Location

SRID = 4326


def point(location: Location) -> ColumnElement[Any]:
    """SQL expression for a WGS84 geography point. PostGIS takes (longitude, latitude)."""
    return sql_cast(
        func.ST_SetSRID(func.ST_MakePoint(location.longitude, location.latitude), SRID),
        Geography("POINT", srid=SRID),
    )


def longitude_of(column: Any) -> Any:
    return ST_X(sql_cast(column, Geometry("POINT", srid=SRID)))


def latitude_of(column: Any) -> Any:
    return ST_Y(sql_cast(column, Geometry("POINT", srid=SRID)))
