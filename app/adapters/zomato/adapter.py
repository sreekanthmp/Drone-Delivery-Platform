"""Zomato adapter. ILLUSTRATIVE format, not Zomato's real contract.

Inbound::

    {"order": {"id": 99812,
               "pickup_point": {"coordinates": [77.59, 12.97]},
               "drop_point":   {"coordinates": [77.62, 12.93]},
               "package_weight_grams": 1200,
               "tags": ["fragile"]}}

Mapping: ``coordinates`` are ``[longitude, latitude]``; weight is grams -> kg; the numeric
order ``id`` becomes the string external order id and is rendered back as a number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from app.adapters.base import HTTP_STATUS_FOR_ERROR, ClientError, ClientResponse
from app.adapters.common import iso, truncate_description, validate_payload
from app.adapters.zomato.schemas import ZomatoOrderPayload
from app.domain.models import AuditEvent, Location, Order, OrderRequest, OrderStatus, Package

STATE_NAMES: Final[Mapping[OrderStatus, str]] = {
    OrderStatus.CREATED: "accepted",
    OrderStatus.ASSIGNED: "dispatch_assigned",
    OrderStatus.PICKED_UP: "picked_up",
    OrderStatus.IN_FLIGHT: "in_transit",
    OrderStatus.ARRIVING: "arriving",
    OrderStatus.DELIVERED: "delivered",
    OrderStatus.FAILED: "failed",
    OrderStatus.CANCELLED: "cancelled",
    OrderStatus.RETURNED: "returned",
}


def _public_id(external_order_id: str) -> int | str:
    """Zomato's ids are numeric; render them back as numbers."""
    return int(external_order_id) if external_order_id.isdigit() else external_order_id


class ZomatoAdapter:
    client_id = "zomato"

    def parse_order(self, payload: Mapping[str, Any]) -> OrderRequest:
        order = validate_payload(ZomatoOrderPayload, payload).order
        pickup_lon, pickup_lat = order.pickup_point.coordinates
        drop_lon, drop_lat = order.drop_point.coordinates
        return OrderRequest(
            client_id=self.client_id,
            external_order_id=str(order.id),
            pickup=Location(latitude=pickup_lat, longitude=pickup_lon),
            dropoff=Location(latitude=drop_lat, longitude=drop_lon),
            package=Package(
                weight_kg=order.package_weight_grams / 1000,
                description=truncate_description(", ".join(order.tags)),
            ),
        )

    def render_order(self, order: Order, *, created: bool) -> ClientResponse:
        return ClientResponse(
            status_code=201 if created else 200,
            body={
                "success": True,
                "order": {
                    "id": _public_id(order.external_order_id),
                    "platform_ref": str(order.id),
                    "state": STATE_NAMES[order.status],
                    "updated_at": iso(order.updated_at),
                },
            },
        )

    def render_status_update(self, order: Order, event: AuditEvent) -> Mapping[str, Any]:
        return {
            "order_id": _public_id(order.external_order_id),
            "state": STATE_NAMES[event.new_state or order.status],
            "event_ts": iso(event.occurred_at),
            "event_ref": str(event.event_id),
        }

    def render_error(self, error: ClientError) -> ClientResponse:
        return ClientResponse(
            status_code=HTTP_STATUS_FOR_ERROR[error.code],
            body={
                "success": False,
                "error": {
                    "type": error.code.value,
                    "message": error.message,
                    "fields": [
                        {"path": issue.path, "message": issue.message} for issue in error.issues
                    ],
                },
            },
        )
