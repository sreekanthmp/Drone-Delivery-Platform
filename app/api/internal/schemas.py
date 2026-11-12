"""Response schemas of the internal API (platform format, not any partner's)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.models import Location, Order, OrderStatus, Package


class LocationOut(BaseModel):
    latitude: float
    longitude: float

    @classmethod
    def from_domain(cls, location: Location) -> LocationOut:
        return cls(latitude=location.latitude, longitude=location.longitude)


class PackageOut(BaseModel):
    weight_kg: float
    description: str

    @classmethod
    def from_domain(cls, package: Package) -> PackageOut:
        return cls(weight_kg=package.weight_kg, description=package.description)


class OrderOut(BaseModel):
    id: UUID
    client_id: str
    external_order_id: str
    status: OrderStatus
    pickup: LocationOut
    dropoff: LocationOut
    package: PackageOut
    drone_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, order: Order) -> OrderOut:
        return cls(
            id=order.id,
            client_id=order.client_id,
            external_order_id=order.external_order_id,
            status=order.status,
            pickup=LocationOut.from_domain(order.pickup),
            dropoff=LocationOut.from_domain(order.dropoff),
            package=PackageOut.from_domain(order.package),
            drone_id=order.drone_id,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class OrderStatusOut(BaseModel):
    order_id: UUID
    status: OrderStatus
    is_terminal: bool
    updated_at: datetime

    @classmethod
    def from_domain(cls, order: Order) -> OrderStatusOut:
        return cls(
            order_id=order.id,
            status=order.status,
            is_terminal=order.status.is_terminal,
            updated_at=order.updated_at,
        )
