"""Internal live streams, in the platform's own format.

``/ws/internal/fleet``: a snapshot of every drone (with its latest sample) and the standing
alerts, then live samples, committed changes and alerts as they happen.

``/ws/internal/orders/{order_id}``: a snapshot of one order, then its committed changes and
the position of the drone carrying it.

Unauthenticated by design (none has been requested): never expose these publicly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket

from app.api.internal.schemas import AlertOut, DroneOut, OrderOut
from app.api.ws.streaming import (
    CLOSE_NOT_FOUND,
    CLOSE_TRY_AGAIN,
    Out,
    refuse,
    run_stream,
)
from app.api.ws.tracker import PLATFORM_VISIBLE, OrderTracker
from app.dispatch.service import DispatchService
from app.domain.models import LIVE_FLIGHT_STATES
from app.realtime.channels import FLEET, order_channel
from app.realtime.models import Envelope
from app.realtime.ports import LiveView
from app.services.exceptions import OrderNotFoundError
from app.services.order_service import OrderService

router = APIRouter(tags=["live"])

UNAVAILABLE = {"type": "error", "code": "live_view_unavailable"}


@router.websocket("/ws/internal/fleet")
async def fleet_stream(websocket: WebSocket) -> None:
    state = websocket.app.state
    live: LiveView | None = state.realtime
    if live is None or not state.hub.connected:
        await refuse(websocket, CLOSE_TRY_AGAIN, "the live view is unavailable", UNAVAILABLE)
        return
    dispatch: DispatchService = state.dispatch_service

    # Subscribe first, then read the snapshot: nothing can fall between the two.
    subscription = state.hub.subscribe([FLEET])
    try:
        drones = await dispatch.list_drones()
        samples = await live.all_states()
        alerts = await live.active()
    except Exception:
        subscription.close()
        await refuse(websocket, CLOSE_TRY_AGAIN, "the live view is unavailable", UNAVAILABLE)
        return

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "snapshot",
            "drones": [
                {
                    **DroneOut.from_domain(drone).model_dump(mode="json"),
                    "state": (
                        samples[drone.id].model_dump(mode="json") if drone.id in samples else None
                    ),
                }
                for drone in drones
            ],
            "alerts": [AlertOut.from_domain(a).model_dump(mode="json") for a in alerts],
        }
    )

    async def forward(channel: str, envelope: Envelope) -> Out:
        return Out(({"type": envelope.kind, **envelope.data},))

    await run_stream(websocket, subscription, ping_s=state.ws_ping_s, on_message=forward)


@router.websocket("/ws/internal/orders/{order_id}")
async def order_stream(websocket: WebSocket, order_id: UUID) -> None:
    state = websocket.app.state
    live: LiveView | None = state.realtime
    if live is None or not state.hub.connected:
        await refuse(websocket, CLOSE_TRY_AGAIN, "the live view is unavailable", UNAVAILABLE)
        return
    orders: OrderService = state.order_service
    dispatch: DispatchService = state.dispatch_service
    try:
        order = await orders.get_order(order_id)
    except OrderNotFoundError:
        await refuse(
            websocket,
            CLOSE_NOT_FOUND,
            "order not found",
            {"type": "error", "code": "not_found", "message": "order not found"},
        )
        return

    subscription = state.hub.subscribe([order_channel(order_id)])
    try:
        flights = await dispatch.list_flights_for_order(order_id)
        live_flight = next((f.id for f in flights if f.status in LIVE_FLIGHT_STATES), None)
        tracker = await OrderTracker.load(
            orders, order, live_flight, live, visible=PLATFORM_VISIBLE
        )
        for channel in tracker.channels():
            subscription.add_channel(channel)
    except Exception:
        subscription.close()
        await refuse(websocket, CLOSE_TRY_AGAIN, "the live view is unavailable", UNAVAILABLE)
        return

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "snapshot",
            "order": OrderOut.from_domain(tracker.order).model_dump(mode="json"),
            "flight_id": None if tracker.flight_id is None else str(tracker.flight_id),
            "position": _position(tracker),
        }
    )

    async def forward(channel: str, envelope: Envelope) -> Out:
        change = await tracker.apply(channel, envelope)
        if change.new_channel is not None:
            subscription.add_channel(change.new_channel)
        frames: list[dict[str, Any]] = []
        if change.audit is not None:
            frames.append({"type": "audit", **change.audit})
        if change.position_changed:
            position = _position(tracker)
            if position is not None:
                frames.append({"type": "position", **position})
        return Out(frames)

    await run_stream(websocket, subscription, ping_s=state.ws_ping_s, on_message=forward)


def _position(tracker: OrderTracker) -> dict[str, Any] | None:
    sample = tracker.state
    if sample is None or tracker.position is None:
        return None
    return {
        "latitude": sample.latitude,
        "longitude": sample.longitude,
        "altitude_m": sample.altitude_m,
        "battery_percent": sample.battery_percent,
        "phase": sample.phase.value,
        "at": sample.received_at.isoformat(),
    }
