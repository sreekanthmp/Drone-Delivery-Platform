"""Order application service.

The only caller of ``OrderStore.insert`` / ``apply_transition``. Every status
change goes through :class:`OrderStateMachine`; the service never builds or
mutates an order itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from app.domain.models import (
    AuditEvent,
    EventSource,
    Location,
    Order,
    OrderStatus,
    Package,
)
from app.domain.order_state_machine import OrderStateMachine
from app.services.exceptions import OrderNotFoundError
from app.services.ports import UnitOfWorkFactory

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    order: Order
    created: bool
    """``False`` when the partner's ``(client_id, external_order_id)`` already existed."""


class OrderService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        machine: OrderStateMachine | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._machine = machine or OrderStateMachine()

    async def create_order(
        self,
        *,
        client_id: str,
        external_order_id: str,
        pickup: Location,
        dropoff: Location,
        package: Package,
        source: EventSource,
        metadata: Mapping[str, Any] | None = None,
    ) -> CreateOrderResult:
        """Create an order, idempotently on ``(client_id, external_order_id)``.

        A repeat of the same key returns the existing order unchanged and
        writes nothing. The payload of the repeat is not compared.
        """
        async with self._uow_factory() as uow:
            existing = await uow.orders.get_by_external_id(client_id, external_order_id)
            if existing is not None:
                logger.info(
                    "order_create_duplicate", order_id=str(existing.id), client_id=client_id
                )
                return CreateOrderResult(order=existing, created=False)

            result = self._machine.create(
                client_id=client_id,
                external_order_id=external_order_id,
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=source,
                metadata=metadata,
            )
            if not await uow.orders.insert(result):
                # Lost a race with a concurrent request carrying the same key.
                winner = await uow.orders.get_by_external_id(client_id, external_order_id)
                if winner is None:  # pragma: no cover - the conflicting row cannot vanish
                    raise RuntimeError("order insert conflicted but no existing order was found")
                logger.info("order_create_duplicate", order_id=str(winner.id), client_id=client_id)
                return CreateOrderResult(order=winner, created=False)

            await uow.commit()

        logger.info(
            "order_created",
            order_id=str(result.order.id),
            client_id=client_id,
            source=source.value,
        )
        return CreateOrderResult(order=result.order, created=True)

    async def transition(
        self,
        order_id: UUID,
        target: OrderStatus,
        *,
        source: EventSource,
        drone_id: UUID | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Order:
        """Move an order to ``target`` via the state machine, recording the audit event.

        The order row is locked first, so concurrent transitions on one order are
        serialised: the loser sees the new status and is rejected by the state
        machine with a domain error.

        Raises:
            OrderNotFoundError: no such order.
            DomainError: the transition is not allowed (nothing is written).
        """
        async with self._uow_factory() as uow:
            order = await uow.orders.get(order_id, for_update=True)
            if order is None:
                raise OrderNotFoundError(order_id)

            result = self._machine.transition(
                order, target, source=source, drone_id=drone_id, metadata=metadata
            )
            await uow.orders.apply_transition(result)
            await uow.commit()

        logger.info(
            "order_transitioned",
            order_id=str(order_id),
            previous_state=order.status.value,
            new_state=target.value,
            source=source.value,
        )
        return result.order

    async def get_order(self, order_id: UUID) -> Order:
        async with self._uow_factory() as uow:
            order = await uow.orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def get_order_by_external_id(self, client_id: str, external_order_id: str) -> Order:
        """Look an order up by the partner's own identifier."""
        async with self._uow_factory() as uow:
            order = await uow.orders.get_by_external_id(client_id, external_order_id)
        if order is None:
            raise OrderNotFoundError(client_id=client_id, external_order_id=external_order_id)
        return order

    async def get_audit_trail(self, order_id: UUID) -> list[AuditEvent]:
        """The order's audit events, oldest first."""
        async with self._uow_factory() as uow:
            if await uow.orders.get(order_id) is None:
                raise OrderNotFoundError(order_id)
            return await uow.audit.list_for_order(order_id)
