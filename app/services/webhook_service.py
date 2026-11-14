"""Webhook delivery service: what operations may see, and the one thing they may ask for.

Reads the queue of notifications owed to partners and the history of attempts at each, and can
give a *failed* delivery another round of attempts. It does not send anything (the sender
process does), and it cannot touch an order: a delivery reports a change, it never causes one
(Rule 4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.domain.models import DeliveryAttempt, DeliveryStatus, WebhookDelivery
from app.services.exceptions import (
    DeliveryNotFoundError,
    DeliveryNotRetryableError,
    OrderNotFoundError,
)
from app.services.ports import UnitOfWorkFactory

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryDetail:
    delivery: WebhookDelivery
    attempts: list[DeliveryAttempt]
    """Every HTTP attempt at the delivery, oldest first."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WebhookService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def list_deliveries(
        self,
        *,
        status: DeliveryStatus | None = None,
        client_id: str | None = None,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        """Deliveries, newest first, optionally of one status and/or one client."""
        async with self._uow_factory() as uow:
            return await uow.webhooks.list_recent(status=status, client_id=client_id, limit=limit)

    async def get_delivery(self, event_id: UUID) -> DeliveryDetail:
        async with self._uow_factory() as uow:
            delivery = await uow.webhooks.get(event_id)
            if delivery is None:
                raise DeliveryNotFoundError(event_id)
            return DeliveryDetail(delivery, await uow.webhooks.attempts_for(event_id))

    async def deliveries_for_order(self, order_id: UUID) -> list[WebhookDelivery]:
        """Every delivery of an order's updates, oldest first."""
        async with self._uow_factory() as uow:
            if await uow.orders.get(order_id) is None:
                raise OrderNotFoundError(order_id)
            return await uow.webhooks.list_for_order(order_id)

    async def retry(self, event_id: UUID) -> WebhookDelivery:
        """Give a failed delivery another round of attempts, starting now.

        Raises:
            DeliveryNotFoundError: no such delivery.
            DeliveryNotRetryableError: it is not failed, or someone else changed it first.
        """
        async with self._uow_factory() as uow:
            delivery = await uow.webhooks.get(event_id)
            if delivery is None:
                raise DeliveryNotFoundError(event_id)
            if delivery.status is not DeliveryStatus.FAILED:
                raise DeliveryNotRetryableError(event_id, delivery.status.value)
            requeued = delivery.requeued(now=self._clock())
            if not await uow.webhooks.apply(delivery, requeued, None):
                current = await uow.webhooks.get(event_id)
                raise DeliveryNotRetryableError(
                    event_id, "changing" if current is None else current.status.value
                )
            await uow.commit()
        logger.info(
            "webhook_requeued",
            event_id=str(event_id),
            client_id=delivery.client_id,
            attempts_so_far=delivery.attempts,
        )
        return requeued
