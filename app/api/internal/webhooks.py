"""Internal webhook endpoints: how notifications to partners are going.

Read-only, except for one verb: ``retry`` gives a *failed* delivery another round of attempts.
Nothing here accepts a status, sends a request to a partner (the sender process does that) or
touches an order (Rule 4); a delivery reports a change, it never causes one.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import WebhookServiceDep
from app.api.internal.schemas import DeliveryDetailOut, DeliveryOut
from app.domain.models import DeliveryStatus

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/webhooks/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    service: WebhookServiceDep,
    status: DeliveryStatus | None = None,
    client_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DeliveryOut]:
    """Deliveries, newest first. ``status=failed`` is the list of notifications that gave up."""
    deliveries = await service.list_deliveries(status=status, client_id=client_id, limit=limit)
    return [DeliveryOut.from_domain(d) for d in deliveries]


@router.get("/webhooks/deliveries/{event_id}", response_model=DeliveryDetailOut)
async def get_delivery(event_id: UUID, service: WebhookServiceDep) -> DeliveryDetailOut:
    """One delivery with every attempt made at it (status code, error and timing of each)."""
    return DeliveryDetailOut.from_domain(await service.get_delivery(event_id))


@router.post("/webhooks/deliveries/{event_id}/retry", response_model=DeliveryOut)
async def retry_delivery(event_id: UUID, service: WebhookServiceDep) -> DeliveryOut:
    """Give a failed delivery another full round of attempts, starting now.

    Answers 409 unless the delivery is ``failed``. The partner may receive the update again
    (it carries the same event id, so a partner that de-duplicates will drop a repeat).
    """
    return DeliveryOut.from_domain(await service.retry(event_id))


@router.get("/orders/{order_id}/webhooks", response_model=list[DeliveryOut])
async def list_order_deliveries(order_id: UUID, service: WebhookServiceDep) -> list[DeliveryOut]:
    """Every notification owed to the order's partner, oldest first."""
    return [DeliveryOut.from_domain(d) for d in await service.deliveries_for_order(order_id)]
