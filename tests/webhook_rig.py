"""A ready-made webhook stack for tests: real order service and sender over any unit of work.

``build_webhook_rig(uow_factory)`` wires the real ``OrderService`` (on a fake clock, so the
queued deliveries are due on the same clock the sender reads), the real ``WebhookSender`` and a
scripted ``FakePoster`` standing in for the partner's server.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.adapters.registry import AdapterRegistry, default_registry
from app.domain.models import (
    DeliveryAttempt,
    EventSource,
    Order,
    OrderStatus,
    Package,
    WebhookDelivery,
)
from app.domain.order_state_machine import OrderStateMachine
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.retry import RetryPolicy, SendResult
from app.webhooks.sender import SenderConfig, WebhookSender
from tests.conftest import DROPOFF, PICKUP, FakeClock

PARTNER_URL = "https://partner.example/hooks"


def ok(code: int = 200) -> SendResult:
    return SendResult(status_code=code, duration_ms=12)


def status(code: int, *, retry_after: float | None = None) -> SendResult:
    return SendResult(status_code=code, duration_ms=15, retry_after_s=retry_after)


def network_error(message: str = "timeout") -> SendResult:
    return SendResult(status_code=None, duration_ms=3000, error=message)


@dataclass(frozen=True)
class Posted:
    url: str
    body: bytes
    headers: dict[str, str]

    @property
    def json(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.body)
        return loaded


class FakePoster:
    """The partner's server: answers from a script, then with ``default``; records every call."""

    def __init__(self, *script: SendResult | Exception, default: SendResult | None = None) -> None:
        self.calls: list[Posted] = []
        self.script: deque[SendResult | Exception] = deque(script)
        self.default = default or ok()
        self.delay_s = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    async def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> SendResult:
        self.calls.append(Posted(url, body, dict(headers)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            answer = self.script.popleft() if self.script else self.default
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.in_flight -= 1

    @property
    def event_ids(self) -> list[str]:
        return [call.headers["X-Event-Id"] for call in self.calls]


@dataclass
class WebhookRig:
    uow_factory: UnitOfWorkFactory
    clock: FakeClock
    orders: OrderService
    poster: FakePoster
    sender: WebhookSender
    registry: AdapterRegistry
    _counter: int = field(default=0)

    async def new_order(self, external_id: str | None = None, *, client_id: str = "mock") -> Order:
        self._counter += 1
        result = await self.orders.create_order(
            client_id=client_id,
            external_order_id=external_id or f"ORD-{self._counter}",
            pickup=PICKUP,
            dropoff=DROPOFF,
            package=Package(weight_kg=1.5, description="meals"),
            source=EventSource.API,
        )
        return result.order

    async def cancel(self, order: Order) -> Order:
        return await self.orders.transition(order.id, OrderStatus.CANCELLED, source=EventSource.API)

    async def deliveries(self, order: Order | None = None) -> list[WebhookDelivery]:
        """Every delivery (or one order's), oldest first."""
        async with self.uow_factory() as uow:
            if order is not None:
                return await uow.webhooks.list_for_order(order.id)
            return list(reversed(await uow.webhooks.list_recent(limit=1000)))

    async def delivery(self, event_id: UUID) -> WebhookDelivery:
        async with self.uow_factory() as uow:
            found = await uow.webhooks.get(event_id)
        assert found is not None
        return found

    async def attempts(self, event_id: UUID) -> list[DeliveryAttempt]:
        async with self.uow_factory() as uow:
            return await uow.webhooks.attempts_for(event_id)

    async def drain(self, *, max_passes: int = 50) -> int:
        """Run sender passes until nothing is due. Returns how many deliveries were handled."""
        handled = 0
        for _ in range(max_passes):
            count = await self.sender.run_once()
            if count == 0:
                return handled
            handled += count
        raise AssertionError("the sender never ran out of work")


def build_webhook_rig(
    uow_factory: UnitOfWorkFactory,
    *,
    endpoints: Mapping[str, WebhookEndpoint] | None = None,
    policy: RetryPolicy | None = None,
    config: SenderConfig | None = None,
    poster: FakePoster | None = None,
    registry: AdapterRegistry | None = None,
    clock: FakeClock | None = None,
) -> WebhookRig:
    clock = clock or FakeClock()
    poster = poster or FakePoster()
    registry = registry or default_registry()
    orders = OrderService(uow_factory, machine=OrderStateMachine(clock=clock))
    sender = WebhookSender(
        uow_factory,
        registry=registry,
        endpoints=endpoints
        if endpoints is not None
        else {"mock": WebhookEndpoint(url=PARTNER_URL)},
        poster=poster,
        policy=policy or RetryPolicy(),
        config=config,
        clock=clock,
        rng=lambda: 0.5,  # no jitter: with 0.5 the random factor is exactly 1
    )
    return WebhookRig(uow_factory, clock, orders, poster, sender, registry)
