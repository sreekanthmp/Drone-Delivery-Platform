"""Inbound payload of the Swiggy integration.

ILLUSTRATIVE: this is not Swiggy's real contract. Replace this module and the mapping in
``adapter.py`` when the real specification is available.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, Strict

from app.adapters.common import Latitude, Longitude, PartnerId, PartnerModel, PositiveNumber

MAX_ITEMS = 100

ItemName = Annotated[str, Strict(), Field(min_length=1, max_length=100)]
Quantity = Annotated[int, Strict(), Field(ge=1, le=1000)]


class SwiggyPoint(PartnerModel):
    latitude: Latitude
    longitude: Longitude


class SwiggyRestaurant(PartnerModel):
    location: SwiggyPoint


class SwiggyCustomer(PartnerModel):
    address: SwiggyPoint


class SwiggyItem(PartnerModel):
    name: ItemName
    quantity: Quantity
    weight_grams: PositiveNumber


class SwiggyOrderPayload(PartnerModel):
    swiggy_order_id: PartnerId
    restaurant: SwiggyRestaurant
    customer: SwiggyCustomer
    items: Annotated[list[SwiggyItem], Field(min_length=1, max_length=MAX_ITEMS)]
