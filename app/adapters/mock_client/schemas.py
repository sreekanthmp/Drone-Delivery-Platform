"""Inbound payload of the mock client: a flat, generic order format."""

from __future__ import annotations

from app.adapters.common import (
    Description,
    Latitude,
    Longitude,
    PartnerId,
    PartnerModel,
    PositiveNumber,
)


class MockPoint(PartnerModel):
    lat: Latitude
    lng: Longitude


class MockPackage(PartnerModel):
    weight_kg: PositiveNumber
    description: Description = ""


class MockOrderPayload(PartnerModel):
    order_id: PartnerId
    pickup: MockPoint
    dropoff: MockPoint
    package: MockPackage
