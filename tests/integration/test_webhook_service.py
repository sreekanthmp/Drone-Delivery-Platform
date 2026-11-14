"""The webhook service (what operations see and may ask for), on both backends."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest

from app.domain.models import AttemptOutcome, DeliveryStatus
from app.services.exceptions import (
    DeliveryNotFoundError,
    DeliveryNotRetryableError,
    OrderNotFoundError,
)
from app.services.ports import UnitOfWork, UnitOfWorkFactory
from app.services.webhook_service import WebhookService
from app.webhooks.retry import RetryPolicy
from tests.webhook_rig import WebhookRig, build_webhook_rig, status

D = DeliveryStatus


@pytest.fixture
def rig(uow_factory: UnitOfWorkFactory) -> WebhookRig:
    return build_webhook_rig(
        uow_factory, policy=RetryPolicy(max_attempts=2, base_s=1, factor=1, jitter=0.0)
    )


@pytest.fixture
def service(rig: WebhookRig, uow_factory: UnitOfWorkFactory) -> WebhookService:
    return WebhookService(uow_factory, clock=rig.clock)


async def failed_delivery(rig: WebhookRig) -> UUID:
    """An order whose only notification the partner rejected for good."""
    rig.poster.script.append(status(400))
    order = await rig.new_order()
    await rig.drain()
    (delivery,) = await rig.deliveries(order)
    assert delivery.status is D.FAILED
    return delivery.event_id


class TestReading:
    async def test_deliveries_are_listed_newest_first_with_filters(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        first = await rig.new_order("A")
        second = await rig.new_order("B", client_id="zomato")
        third = await rig.new_order("C")
        await rig.drain()

        everyone = await service.list_deliveries()
        assert [d.order_id for d in everyone] == [third.id, second.id, first.id]
        assert [d.order_id for d in await service.list_deliveries(limit=1)] == [third.id]
        assert [d.order_id for d in await service.list_deliveries(client_id="zomato")] == [
            second.id
        ]
        assert {d.status for d in await service.list_deliveries(status=D.DELIVERED)} == {
            D.DELIVERED
        }
        assert await service.list_deliveries(status=D.FAILED) == []

    async def test_a_delivery_comes_with_every_attempt_at_it(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        rig.poster.script.append(status(503))
        order = await rig.new_order()
        await rig.drain()
        rig.clock.advance(seconds=1)
        await rig.drain()
        (delivery,) = await rig.deliveries(order)

        detail = await service.get_delivery(delivery.event_id)

        assert detail.delivery.status is D.DELIVERED
        assert [(a.attempt_no, a.outcome, a.status_code) for a in detail.attempts] == [
            (1, AttemptOutcome.RETRY, 503),
            (2, AttemptOutcome.DELIVERED, 200),
        ]

    async def test_an_orders_deliveries_are_listed_oldest_first(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        order = await rig.new_order()
        await rig.cancel(order)
        trail = await rig.orders.get_audit_trail(order.id)

        deliveries = await service.deliveries_for_order(order.id)

        assert [d.event_id for d in deliveries] == [e.event_id for e in trail]

    async def test_an_unknown_delivery(self, service: WebhookService) -> None:
        with pytest.raises(DeliveryNotFoundError, match="not found"):
            await service.get_delivery(uuid4())

    async def test_an_unknown_order(self, service: WebhookService) -> None:
        with pytest.raises(OrderNotFoundError):
            await service.deliveries_for_order(uuid4())

    async def test_an_order_always_has_its_created_update_queued(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        order = await rig.new_order()

        (delivery,) = await service.deliveries_for_order(order.id)

        assert (delivery.status, delivery.order_id) == (D.PENDING, order.id)


class TestRetrying:
    async def test_a_failed_delivery_gets_a_fresh_round_starting_now(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        event_id = await failed_delivery(rig)
        rig.clock.advance(hours=3)

        again = await service.retry(event_id)

        assert again.status is D.PENDING
        assert again.next_attempt_at == rig.clock.now
        assert (again.attempts, again.round_start) == (1, 1)
        stored = await rig.delivery(event_id)
        assert (stored.status, stored.round_start) == (D.PENDING, 1)

    async def test_the_sender_then_sends_it_again_with_the_same_event_id(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        event_id = await failed_delivery(rig)
        before = len(rig.poster.calls)

        await service.retry(event_id)
        await rig.drain()

        delivery = await rig.delivery(event_id)
        assert (delivery.status, delivery.attempts) == (D.DELIVERED, 2)
        assert rig.poster.event_ids[before:] == [str(event_id)]
        attempts = await rig.attempts(event_id)
        assert [a.attempt_no for a in attempts] == [1, 2]  # history kept, numbering continues

    async def test_the_new_round_has_its_own_attempt_budget(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        event_id = await failed_delivery(rig)
        rig.poster.default = status(503)

        await service.retry(event_id)
        await rig.drain()  # attempt 2 (1 in the new round): retry, not failed
        assert (await rig.delivery(event_id)).status is D.PENDING
        rig.clock.advance(seconds=1)
        await rig.drain()  # attempt 3 (2 in the new round): gives up again

        assert (await rig.delivery(event_id)).status is D.FAILED

    @pytest.mark.parametrize("how", ["pending", "delivered", "skipped"])
    async def test_only_a_failed_delivery_can_be_retried(
        self, rig: WebhookRig, service: WebhookService, uow_factory: UnitOfWorkFactory, how: str
    ) -> None:
        if how == "skipped":
            rig = build_webhook_rig(uow_factory, endpoints={})
            service = WebhookService(uow_factory, clock=rig.clock)
        order = await rig.new_order()
        if how != "pending":
            await rig.drain()
        (delivery,) = await rig.deliveries(order)
        assert delivery.status.value == how

        with pytest.raises(DeliveryNotRetryableError, match=f"is {how}"):
            await service.retry(delivery.event_id)

        assert (await rig.delivery(delivery.event_id)) == delivery  # untouched

    async def test_a_second_retry_is_refused_because_it_is_already_pending(
        self, rig: WebhookRig, service: WebhookService
    ) -> None:
        event_id = await failed_delivery(rig)
        await service.retry(event_id)

        with pytest.raises(DeliveryNotRetryableError, match="is pending"):
            await service.retry(event_id)

    async def test_an_unknown_delivery_cannot_be_retried(self, service: WebhookService) -> None:
        with pytest.raises(DeliveryNotFoundError):
            await service.retry(uuid4())

    async def test_losing_a_race_with_someone_else_is_a_refusal_not_a_double_requeue(
        self, rig: WebhookRig, uow_factory: UnitOfWorkFactory
    ) -> None:
        event_id = await failed_delivery(rig)

        class LosesEveryApply:
            """A unit of work whose compare-and-set always reports that someone got there first."""

            def __init__(self, inner: UnitOfWork) -> None:
                self._inner = inner

            async def __aenter__(self) -> Self:
                await self._inner.__aenter__()
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> None:
                await self._inner.__aexit__(exc_type, exc, tb)

            @property
            def webhooks(self) -> Any:
                inner = self._inner.webhooks

                class Losing:
                    async def apply(self, *args: Any) -> bool:
                        return False

                    def __getattr__(self, name: str) -> Any:
                        return getattr(inner, name)

                return Losing()

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        service = WebhookService(lambda: LosesEveryApply(uow_factory()), clock=rig.clock)  # type: ignore[arg-type, return-value]

        with pytest.raises(DeliveryNotRetryableError):
            await service.retry(event_id)

        assert (await rig.delivery(event_id)).status is D.FAILED  # nothing was changed
