"""Mock client adapter: a generic integration for development and tests.

Inbound::

    {"order_id": "ORD-1",
     "pickup":  {"lat": 12.97, "lng": 77.59},
     "dropoff": {"lat": 12.93, "lng": 77.62},
     "package": {"weight_kg": 1.5, "description": "2 meals"}}

Status vocabulary: the platform's own names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from app.adapters.base import HTTP_STATUS_FOR_ERROR, ClientError, ClientResponse
from app.adapters.common import iso, validate_payload
from app.adapters.mock_client.schemas import MockOrderPayload
from app.domain.models import AuditEvent, Location, Order, OrderRequest, OrderStatus, Package

STATUS_NAMES: Final[Mapping[OrderStatus, str]] = {status: status.value for status in OrderStatus}


class MockClientAdapter:
    client_id = "mock"

    def parse_order(self, payload: Mapping[str, Any]) -> OrderRequest:
        data = validate_payload(MockOrderPayload, payload)
        return OrderRequest(
            client_id=self.client_id,
            external_order_id=data.order_id,
            pickup=Location(latitude=data.pickup.lat, longitude=data.pickup.lng),
            dropoff=Location(latitude=data.dropoff.lat, longitude=data.dropoff.lng),
            package=Package(weight_kg=data.package.weight_kg, description=data.package.description),
        )

    def render_order(self, order: Order, *, created: bool) -> ClientResponse:
        return ClientResponse(
            status_code=201 if created else 200,
            body={
                "order_id": order.external_order_id,
                "tracking_id": str(order.id),
                "status": STATUS_NAMES[order.status],
                "created_at": iso(order.created_at),
                "updated_at": iso(order.updated_at),
            },
        )

    def render_status_update(self, order: Order, event: AuditEvent) -> Mapping[str, Any]:
        return {
            "order_id": order.external_order_id,
            "status": STATUS_NAMES[event.new_state or order.status],
            "event_id": str(event.event_id),
            "occurred_at": iso(event.occurred_at),
        }

    def render_error(self, error: ClientError) -> ClientResponse:
        return ClientResponse(
            status_code=HTTP_STATUS_FOR_ERROR[error.code],
            body={
                "error": {
                    "code": error.code.value,
                    "message": error.message,
                    "details": [
                        {"field": issue.path, "message": issue.message} for issue in error.issues
                    ],
                }
            },
        )
