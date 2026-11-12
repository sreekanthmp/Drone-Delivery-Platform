"""Inbound payload of the Zomato integration.

ILLUSTRATIVE: this is not Zomato's real contract. Replace this module and the mapping in
``adapter.py`` when the real specification is available.

Coordinates are GeoJSON-style ``[longitude, latitude]``, the opposite order to the
platform's ``Location(latitude, longitude)``, which is exactly the kind of difference an
adapter exists to absorb.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, Strict

from app.adapters.common import Latitude, Longitude, PartnerModel, PositiveNumber

Tag = Annotated[str, Strict(), Field(min_length=1, max_length=50)]
OrderNumber = Annotated[int, Strict(), Field(gt=0)]


class ZomatoPoint(PartnerModel):
    coordinates: tuple[Longitude, Latitude]


class ZomatoOrder(PartnerModel):
    id: OrderNumber
    pickup_point: ZomatoPoint
    drop_point: ZomatoPoint
    package_weight_grams: PositiveNumber
    tags: Annotated[list[Tag], Field(max_length=20)] = Field(default_factory=list)


class ZomatoOrderPayload(PartnerModel):
    order: ZomatoOrder
