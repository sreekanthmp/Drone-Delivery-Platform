"""Swiggy adapter. ILLUSTRATIVE format, not Swiggy's real contract.

Inbound::

    {"swiggy_order_id": "SW-100234",
     "restaurant": {"location": {"latitude": 12.97, "longitude": 77.59}},
     "customer": {"address": {"latitude": 12.93, "longitude": 77.62}},
     "items": [{"name": "Biryani", "quantity": 2, "weight_grams": 450}]}

Mapping: restaurant location -> pickup, customer address -> dropoff, package weight =
sum(quantity x weight_grams) / 1000 kg.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from app.adapters.base import HTTP_STATUS_FOR_ERROR, ClientError, ClientResponse
from app.adapters.common import iso, order_status_of, truncate_description, validate_payload
from app.adapters.swiggy.schemas import SwiggyOrderPayload
from app.domain.models import AuditEvent, Location, Order, OrderRequest, OrderStatus, Package

STATUS_NAMES: Final[Mapping[OrderStatus, str]] = {
    OrderStatus.CREATED: "PLACED",
    OrderStatus.ASSIGNED: "DRONE_ASSIGNED",
    OrderStatus.PICKED_UP: "PICKED_UP",
    OrderStatus.IN_FLIGHT: "OUT_FOR_DELIVERY",
    OrderStatus.ARRIVING: "ARRIVING",
    OrderStatus.DELIVERED: "DELIVERED",
    OrderStatus.FAILED: "DELIVERY_FAILED",
    OrderStatus.CANCELLED: "CANCELLED",
    OrderStatus.RETURNED: "RETURNED_TO_RESTAURANT",
}


class SwiggyAdapter:
    client_id = "swiggy"

    def parse_order(self, payload: Mapping[str, Any]) -> OrderRequest:
        data = validate_payload(SwiggyOrderPayload, payload)
        grams = sum(item.quantity * item.weight_grams for item in data.items)
        description = ", ".join(f"{item.quantity} x {item.name}" for item in data.items)
        return OrderRequest(
            client_id=self.client_id,
            external_order_id=data.swiggy_order_id,
            pickup=Location(
                latitude=data.restaurant.location.latitude,
                longitude=data.restaurant.location.longitude,
            ),
            dropoff=Location(
                latitude=data.customer.address.latitude,
                longitude=data.customer.address.longitude,
            ),
            package=Package(weight_kg=grams / 1000, description=truncate_description(description)),
        )

    def render_order(self, order: Order, *, created: bool) -> ClientResponse:
        return ClientResponse(
            status_code=201 if created else 200,
            body={
                "status": "success",
                "data": {
                    "swiggy_order_id": order.external_order_id,
                    "drone_order_ref": str(order.id),
                    "order_status": STATUS_NAMES[order.status],
                    "last_updated": iso(order.updated_at),
                },
            },
        )

    def render_status_update(self, order: Order, event: AuditEvent) -> Mapping[str, Any]:
        return {
            "swiggy_order_id": order.external_order_id,
            "event": STATUS_NAMES[order_status_of(order, event)],
            "timestamp": iso(event.occurred_at),
            "reference": str(event.event_id),
        }

    def render_error(self, error: ClientError) -> ClientResponse:
        return ClientResponse(
            status_code=HTTP_STATUS_FOR_ERROR[error.code],
            body={
                "status": "error",
                "error_code": error.code.value.upper(),
                "message": error.message,
                "errors": [
                    {"field": issue.path, "message": issue.message} for issue in error.issues
                ],
            },
        )
