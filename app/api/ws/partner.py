"""The partner live stream (Rule 3): every frame is rendered by that partner's own adapter.

``/ws/partner/{client_id}/orders/{external_order_id}`` sends the order's status in the
partner's vocabulary and, only while a drone is carrying the package (picked up, in flight,
arriving), where it is. It never carries the drone, the flight or the battery: the adapter is
given a ``TrackingPosition``, which has no room for them. The stream ends when the order does.

Unauthenticated by design (none has been requested): partners are identified only by the
``client_id`` in the URL. Do not expose it publicly.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.adapters.base import ClientAdapter, ClientError, ErrorCode
from app.adapters.registry import AdapterRegistry, UnknownClientError
from app.api.ws.streaming import (
    CLOSE_NOT_FOUND,
    CLOSE_TRY_AGAIN,
    Out,
    refuse,
    run_stream,
)
from app.api.ws.tracker import PARTNER_VISIBLE, OrderTracker
from app.dispatch.service import DispatchService
from app.domain.models import LIVE_FLIGHT_STATES
from app.realtime.channels import order_channel
from app.realtime.models import Envelope
from app.realtime.ports import LiveView
from app.services.exceptions import OrderNotFoundError
from app.services.order_service import OrderService

router = APIRouter(tags=["live"])


@router.websocket("/ws/partner/{client_id}/orders/{external_order_id}")
async def partner_stream(websocket: WebSocket, client_id: str, external_order_id: str) -> None:
    state = websocket.app.state
    registry: AdapterRegistry = state.adapter_registry
    try:
        adapter: ClientAdapter = registry.get(client_id)
    except UnknownClientError:
        await refuse(
            websocket,
            CLOSE_NOT_FOUND,
            "unknown client",
            {"error": {"code": "unknown_client", "message": f"unknown client '{client_id}'"}},
        )
        return

    live: LiveView | None = state.realtime
    if live is None or not state.hub.connected:
        await refuse(
            websocket,
            CLOSE_TRY_AGAIN,
            "the live view is unavailable",
            adapter.render_error(
                ClientError(ErrorCode.UNAVAILABLE, "live tracking is temporarily unavailable")
            ).body,
        )
        return

    orders: OrderService = state.order_service
    dispatch: DispatchService = state.dispatch_service
    try:
        order = await orders.get_order_by_external_id(client_id, external_order_id)
    except OrderNotFoundError:
        await refuse(
            websocket,
            CLOSE_NOT_FOUND,
            "order not found",
            adapter.render_error(ClientError(ErrorCode.NOT_FOUND, "order not found")).body,
        )
        return

    subscription = state.hub.subscribe([order_channel(order.id)])
    try:
        flights = await dispatch.list_flights_for_order(order.id)
        live_flight = next((f.id for f in flights if f.status in LIVE_FLIGHT_STATES), None)
        tracker = await OrderTracker.load(orders, order, live_flight, live, visible=PARTNER_VISIBLE)
        for channel in tracker.channels():
            subscription.add_channel(channel)
    except Exception:
        subscription.close()
        await refuse(
            websocket,
            CLOSE_TRY_AGAIN,
            "the live view is unavailable",
            adapter.render_error(
                ClientError(ErrorCode.UNAVAILABLE, "live tracking is temporarily unavailable")
            ).body,
        )
        return

    await websocket.accept()
    await websocket.send_json(dict(adapter.render_tracking_update(tracker.order, tracker.position)))
    if tracker.order.status.is_terminal:
        await websocket.close(code=1000, reason="finished")
        subscription.close()
        return

    async def forward(channel: str, envelope: Envelope) -> Out:
        change = await tracker.apply(channel, envelope)
        if change.new_channel is not None:
            subscription.add_channel(change.new_channel)
        shows_something_new = change.status_changed or (
            change.position_changed and tracker.position is not None
        )
        if not shows_something_new:
            return Out()  # flight-level detail is internal, and a hidden position is no news
        frame = dict(adapter.render_tracking_update(tracker.order, tracker.position))
        return Out((frame,), finished=tracker.order.status.is_terminal)

    await run_stream(websocket, subscription, ping_s=state.ws_ping_s, on_message=forward)
