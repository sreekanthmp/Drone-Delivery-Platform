"""Partner gateway routes (Rule 3).

Every partner request is handled by that partner's adapter: it parses the payload into
an :class:`OrderRequest`, and renders every response and every error in the partner's own
format. The core services only ever see internal types.

No authentication is implemented here by design (it has not been requested); partners are
identified by the ``client_id`` in the URL only. Do not expose these routes publicly yet.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.adapters.base import (
    AdapterValidationError,
    ClientAdapter,
    ClientError,
    ClientResponse,
    ErrorCode,
    FieldIssue,
)
from app.adapters.registry import AdapterRegistry, UnknownClientError
from app.api.deps import AdapterRegistryDep, DispatchServiceDep, OrderServiceDep
from app.api.errors import error_response
from app.dispatch.exceptions import CommandNotConfirmedError, FlightInProgressError
from app.domain.exceptions import InvalidTransitionError, OrderValidationError
from app.domain.models import EventSource
from app.services.exceptions import ConcurrentUpdateError, OrderNotFoundError

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/partner", tags=["partner"])


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AdapterValidationError(
            [FieldIssue("$", "the request body must be valid JSON")]
        ) from None
    if not isinstance(body, dict):
        raise AdapterValidationError([FieldIssue("$", "the request body must be a JSON object")])
    return body


async def _respond(
    adapter: ClientAdapter, action: Callable[[], Awaitable[ClientResponse]]
) -> JSONResponse:
    """Run ``action`` and turn any failure into an error in the partner's own format."""
    try:
        response = await action()
    except AdapterValidationError as exc:
        response = adapter.render_error(
            ClientError(ErrorCode.INVALID_REQUEST, str(exc), exc.issues)
        )
    except OrderValidationError as exc:
        response = adapter.render_error(ClientError(ErrorCode.INVALID_REQUEST, str(exc)))
    except OrderNotFoundError:
        response = adapter.render_error(ClientError(ErrorCode.NOT_FOUND, "order not found"))
    except InvalidTransitionError as exc:
        response = adapter.render_error(ClientError(ErrorCode.CONFLICT, str(exc)))
    except (ConcurrentUpdateError, FlightInProgressError):
        response = adapter.render_error(
            ClientError(ErrorCode.CONFLICT, "the order is being changed; try again")
        )
    except CommandNotConfirmedError:
        # The drone could not be recalled, so the order was deliberately NOT cancelled.
        response = adapter.render_error(
            ClientError(
                ErrorCode.UNAVAILABLE,
                "the order was not cancelled because the drone could not be recalled; retry",
            )
        )
    except Exception:
        # Last line of defence at the boundary: log everything, tell the partner nothing internal.
        logger.exception("partner_request_failed", client_id=adapter.client_id)
        response = adapter.render_error(ClientError(ErrorCode.INTERNAL_ERROR, "internal error"))
    return JSONResponse(status_code=response.status_code, content=response.body)


def _adapter_or_404(registry: AdapterRegistry, client_id: str) -> ClientAdapter | JSONResponse:
    try:
        return registry.get(client_id)
    except UnknownClientError:
        return error_response(404, "unknown_client", f"unknown client '{client_id}'")


@router.post("/{client_id}/orders")
async def create_order(
    client_id: str,
    request: Request,
    service: OrderServiceDep,
    registry: AdapterRegistryDep,
) -> JSONResponse:
    adapter = _adapter_or_404(registry, client_id)
    if isinstance(adapter, JSONResponse):
        return adapter

    async def action() -> ClientResponse:
        order_request = adapter.parse_order(await _read_json_object(request))
        result = await service.create_order(
            client_id=order_request.client_id,
            external_order_id=order_request.external_order_id,
            pickup=order_request.pickup,
            dropoff=order_request.dropoff,
            package=order_request.package,
            source=EventSource.ADAPTER,
            metadata={"client_id": order_request.client_id},
        )
        return adapter.render_order(result.order, created=result.created)

    return await _respond(adapter, action)


@router.get("/{client_id}/orders/{external_order_id}")
async def get_order(
    client_id: str,
    external_order_id: str,
    service: OrderServiceDep,
    registry: AdapterRegistryDep,
) -> JSONResponse:
    adapter = _adapter_or_404(registry, client_id)
    if isinstance(adapter, JSONResponse):
        return adapter

    async def action() -> ClientResponse:
        order = await service.get_order_by_external_id(client_id, external_order_id)
        return adapter.render_order(order, created=False)

    return await _respond(adapter, action)


@router.post("/{client_id}/orders/{external_order_id}/cancel")
async def cancel_order(
    client_id: str,
    external_order_id: str,
    orders: OrderServiceDep,
    dispatch: DispatchServiceDep,
    registry: AdapterRegistryDep,
) -> JSONResponse:
    """Cancel an order. If its drone is flying it is recalled first.

    Idempotent for an order that is already cancelled. An order that has finished
    (delivered, failed, returned) answers 409 in the partner's error format; if the drone
    cannot be recalled the order is NOT cancelled and the answer is 503 (retry).
    """
    adapter = _adapter_or_404(registry, client_id)
    if isinstance(adapter, JSONResponse):
        return adapter

    async def action() -> ClientResponse:
        order = await orders.get_order_by_external_id(client_id, external_order_id)
        result = await dispatch.cancel_order(
            order.id, source=EventSource.ADAPTER, reason="cancelled by the partner"
        )
        return adapter.render_order(result.order, created=False)

    return await _respond(adapter, action)
