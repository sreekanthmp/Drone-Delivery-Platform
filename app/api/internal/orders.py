"""Internal order endpoints: read an order, list its flights, and dispatch it.

There is deliberately no endpoint here (or anywhere) that accepts a status: orders are
created through an adapter and move only through the state machine (Rule 4). Dispatching is
a verb, not a status write: it runs the Rule 2 workflow and the order moves as a result.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import DispatchServiceDep, OrderServiceDep
from app.api.internal.schemas import DispatchOut, FlightOut, OrderOut, OrderStatusOut
from app.domain.models import EventSource

router = APIRouter(prefix="/internal/orders", tags=["internal"])


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: UUID, service: OrderServiceDep) -> OrderOut:
    return OrderOut.from_domain(await service.get_order(order_id))


@router.get("/{order_id}/status", response_model=OrderStatusOut)
async def get_order_status(order_id: UUID, service: OrderServiceDep) -> OrderStatusOut:
    return OrderStatusOut.from_domain(await service.get_order(order_id))


@router.get("/{order_id}/flights", response_model=list[FlightOut])
async def list_flights(order_id: UUID, service: DispatchServiceDep) -> list[FlightOut]:
    """Every flight of the order, oldest first (a retried dispatch leaves its history)."""
    return [FlightOut.from_domain(f) for f in await service.list_flights_for_order(order_id)]


@router.post("/{order_id}/dispatch", response_model=DispatchOut)
async def dispatch_order(
    order_id: UUID, service: DispatchServiceDep, start: bool = True
) -> DispatchOut:
    """Assign a drone, obtain permission, upload the mission and (by default) start it.

    Answers 200 with an ``outcome`` for every result the workflow handled, including the
    refusals (no drone, permission denied, upload failed, ...): each leaves the system
    consistent and the order retryable, so none is a request error. ``start=false`` stops
    once the mission is on board.
    """
    result = await service.dispatch_order(order_id, start=start, source=EventSource.API)
    return DispatchOut.from_domain(result)
