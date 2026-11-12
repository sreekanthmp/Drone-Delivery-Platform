"""Read-only internal order endpoints.

There is deliberately no endpoint here (or anywhere) that accepts a status: orders are
created through an adapter and move only through the state machine (Rule 4).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import OrderServiceDep
from app.api.internal.schemas import OrderOut, OrderStatusOut

router = APIRouter(prefix="/internal/orders", tags=["internal"])


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: UUID, service: OrderServiceDep) -> OrderOut:
    return OrderOut.from_domain(await service.get_order(order_id))


@router.get("/{order_id}/status", response_model=OrderStatusOut)
async def get_order_status(order_id: UUID, service: OrderServiceDep) -> OrderStatusOut:
    return OrderStatusOut.from_domain(await service.get_order(order_id))
