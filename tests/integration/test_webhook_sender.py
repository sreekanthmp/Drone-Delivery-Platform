"""The webhook sender, end to end over the real order service, on both backends.

The ``memory`` variants always run; ``postgres`` needs PostgreSQL with PostGIS. The partner's
server is a scripted ``FakePoster``; ``tests/api/test_webhook_poster.py`` covers real HTTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

import pytest
import structlog

from app.adapters.registry import AdapterRegistry, default_registry
from app.dispatch.progress import ProgressEvent as P
from app.domain.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    DeliveryStatus,
    EventSource,
    Order,
    Package,
)
from app.services.ports import UnitOfWorkFactory
from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.retry import RetryPolicy
from app.webhooks.sender import SenderConfig, WebhookSender
from app.webhooks.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from tests.conftest import DROPOFF, PICKUP, FakeClock
from tests.dispatch_rig import build_rig
from tests.webhook_rig import (
    PARTNER_URL,
    FakePoster,
    WebhookRig,
    build_webhook_rig,
    network_error,
    ok,
    status,
)

D = DeliveryStatus


@pytest.fixture
def rig(uow_factory: UnitOfWorkFactory) -> WebhookRig:
    return build_webhook_rig(uow_factory)


async def until(condition: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.005)


class TestWhatIsSent:
    async def test_every_order_change_is_sent_in_the_partners_format(self, rig: WebhookRig) -> None:
        order = await rig.new_order("ORD-7")
        await rig.cancel(order)

        assert await rig.drain() == 2

        bodies = [call.json for call in rig.poster.calls]
        assert [body["status"] for body in bodies] == ["created", "cancelled"]
        assert {body["order_id"] for body in bodies} == {"ORD-7"}
        trail = await rig.orders.get_audit_trail(order.id)
        assert [body["event_id"] for body in bodies] == [str(e.event_id) for e in trail]
        assert [d.status for d in await rig.deliveries(order)] == [D.DELIVERED, D.DELIVERED]

    async def test_the_request_carries_the_event_id_as_its_idempotency_key(
        self, rig: WebhookRig
    ) -> None:
        order = await rig.new_order()
        trail = await rig.orders.get_audit_trail(order.id)

        await rig.drain()

        (call,) = rig.poster.calls
        assert call.url == PARTNER_URL
        assert call.headers["X-Event-Id"] == str(trail[0].event_id)
        assert call.headers["X-Delivery-Attempt"] == "1"
        assert call.headers["Content-Type"] == "application/json"
        assert SIGNATURE_HEADER not in call.headers
        assert TIMESTAMP_HEADER not in call.headers

    async def test_the_same_event_id_goes_with_every_attempt(self, rig: WebhookRig) -> None:
        rig.poster.script.extend([status(503), status(503)])
        await rig.new_order()

        await rig.drain()
        rig.clock.advance(seconds=5)
        await rig.drain()
        rig.clock.advance(seconds=15)
        await rig.drain()

        assert len(rig.poster.calls) == 3
        assert len(set(rig.poster.event_ids)) == 1
        assert [c.headers["X-Delivery-Attempt"] for c in rig.poster.calls] == ["1", "2", "3"]
        assert len({c.body for c in rig.poster.calls}) == 1  # the body is identical too

    async def test_a_partner_with_a_secret_gets_a_signature_it_can_verify(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        endpoint = WebhookEndpoint(url=PARTNER_URL, secret="s3cret")  # type: ignore[arg-type]
        rig = build_webhook_rig(uow_factory, endpoints={"mock": endpoint})
        await rig.new_order()

        with structlog.testing.capture_logs() as logs:
            await rig.drain()

        (call,) = rig.poster.calls
        assert verify(
            "s3cret",
            int(call.headers[TIMESTAMP_HEADER]),
            call.body,
            call.headers[SIGNATURE_HEADER],
        )
        assert not verify("other", int(call.headers[TIMESTAMP_HEADER]), call.body, "sha256=00")
        assert "s3cret" not in str(logs)

    async def test_only_order_changes_are_queued_never_flight_events(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        # Real clocks: the database's Rule 2 trigger judges permissions by its own clock.
        dispatch = build_rig(uow_factory)
        webhooks = build_webhook_rig(uow_factory)
        await dispatch.add_drone()
        order = await dispatch.new_order()
        result = await dispatch.dispatch.dispatch_order(order.id)
        assert result.flight is not None

        trail = await dispatch.trail(order.id)
        order_events = [
            e
            for e in trail
            if e.event_type in (AuditEventType.ORDER_CREATED, AuditEventType.ORDER_STATUS_CHANGED)
        ]
        assert len(order_events) < len(trail)  # flight events exist...
        deliveries = await webhooks.deliveries(order)
        assert [d.event_id for d in deliveries] == [
            e.event_id for e in order_events
        ]  # ...unreported


@pytest.mark.parametrize(
    ("client_id", "external_id"),
    [("mock", "ORD-1"), ("swiggy", "SW-100234"), ("zomato", "99812")],
)
async def test_a_partner_never_hears_about_drones_or_flights(
    uow_factory: UnitOfWorkFactory, client_id: str, external_id: str
) -> None:
    dispatch = build_rig(uow_factory)  # real clocks: see the note in the test above
    poster = FakePoster()
    sender = WebhookSender(
        uow_factory,
        registry=default_registry(),
        endpoints={client_id: WebhookEndpoint(url=PARTNER_URL)},
        poster=poster,
    )
    drone = await dispatch.add_drone()
    created = await dispatch.orders.create_order(
        client_id=client_id,
        external_order_id=external_id,
        pickup=PICKUP,
        dropoff=DROPOFF,
        package=Package(weight_kg=1.5, description="meals"),
        source=EventSource.API,
    )
    dispatch.created.append(created.order)
    result = await dispatch.dispatch.dispatch_order(created.order.id)
    assert result.flight is not None
    for event in (
        P.PACKAGE_PICKED_UP,
        P.DEPARTED_PICKUP,
        P.ARRIVING_AT_DROPOFF,
        P.PACKAGE_DELIVERED,
        P.MISSION_COMPLETED,
    ):
        assert (
            await dispatch.dispatch.record_progress(drone.id, result.flight.id, event)
        ).applied, event

    while await sender.run_once():
        pass

    assert len(poster.calls) == 6  # created, assigned, picked up, in flight, arriving, delivered
    for call in poster.calls:
        text = call.body.decode()
        assert str(drone.id) not in text
        assert str(result.flight.id) not in text
        for word in ("battery", "drone_id", "flight_id", "mission", "permission"):
            assert word not in text


class TestWhenThePartnerAnswers:
    async def test_a_2xx_is_delivered_and_recorded(self, rig: WebhookRig) -> None:
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.DELIVERED
        assert (delivery.attempts, delivery.last_status_code) == (1, 200)
        assert delivery.delivered_at == rig.clock.now
        assert delivery.lease_until is None
        (attempt,) = await rig.attempts(delivery.event_id)
        assert (attempt.attempt_no, attempt.outcome, attempt.status_code) == (
            1,
            AttemptOutcome.DELIVERED,
            200,
        )
        assert attempt.duration_ms == 12

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 429])
    async def test_a_server_error_is_tried_again_later(self, rig: WebhookRig, code: int) -> None:
        rig.poster.script.append(status(code))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.PENDING
        assert delivery.attempts == 1
        assert delivery.last_status_code == code
        assert delivery.next_attempt_at == rig.clock.now + timedelta(seconds=5)
        assert delivery.lease_until is None
        (attempt,) = await rig.attempts(delivery.event_id)
        assert attempt.outcome is AttemptOutcome.RETRY

    async def test_it_waits_for_the_retry_time_and_then_succeeds(self, rig: WebhookRig) -> None:
        rig.poster.script.append(status(503))
        order = await rig.new_order()
        await rig.drain()
        assert len(rig.poster.calls) == 1

        rig.clock.advance(seconds=4)
        assert await rig.sender.run_once() == 0  # not due yet
        rig.clock.advance(seconds=1)
        assert await rig.drain() == 1

        (delivery,) = await rig.deliveries(order)
        assert (delivery.status, delivery.attempts) == (D.DELIVERED, 2)
        assert delivery.last_error is None
        outcomes = [a.outcome for a in await rig.attempts(delivery.event_id)]
        assert outcomes == [AttemptOutcome.RETRY, AttemptOutcome.DELIVERED]

    async def test_the_waits_grow_each_time(self, rig: WebhookRig) -> None:
        rig.poster.default = status(503)
        order = await rig.new_order()

        waits = []
        for _ in range(4):
            before = rig.clock.now
            await rig.drain()
            (delivery,) = await rig.deliveries(order)
            waits.append((delivery.next_attempt_at - before).total_seconds())
            rig.clock.now = delivery.next_attempt_at

        assert waits == [5, 15, 45, 135]

    async def test_a_retry_after_longer_than_the_backoff_is_honoured(self, rig: WebhookRig) -> None:
        rig.poster.script.append(status(429, retry_after=120))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.next_attempt_at == rig.clock.now + timedelta(seconds=120)

    async def test_a_retry_after_is_capped(self, uow_factory: UnitOfWorkFactory) -> None:
        rig = build_webhook_rig(uow_factory, policy=RetryPolicy(cap_s=600))
        rig.poster.script.append(status(503, retry_after=86_400))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.next_attempt_at == rig.clock.now + timedelta(seconds=600)

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
    async def test_a_client_error_is_final(self, rig: WebhookRig, code: int) -> None:
        rig.poster.script.append(status(code))
        order = await rig.new_order()

        await rig.drain()
        rig.clock.advance(hours=24)
        assert await rig.sender.run_once() == 0  # nothing is ever retried

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.FAILED
        assert (delivery.attempts, delivery.last_status_code) == (1, code)
        assert delivery.last_error == f"the partner rejected it: HTTP {code}"
        (attempt,) = await rig.attempts(delivery.event_id)
        assert attempt.outcome is AttemptOutcome.FAILED

    async def test_a_redirect_is_never_followed_and_is_final(self, rig: WebhookRig) -> None:
        rig.poster.script.append(status(302))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.FAILED
        assert delivery.last_error is not None
        assert "redirects are not followed" in delivery.last_error
        assert len(rig.poster.calls) == 1

    async def test_no_answer_at_all_is_tried_again(self, rig: WebhookRig) -> None:
        rig.poster.script.append(network_error("timeout"))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.PENDING
        assert (delivery.last_status_code, delivery.last_error) == (None, "timeout")
        (attempt,) = await rig.attempts(delivery.event_id)
        assert (attempt.status_code, attempt.error) == (None, "timeout")

    async def test_it_gives_up_after_the_last_attempt(self, uow_factory: UnitOfWorkFactory) -> None:
        policy = RetryPolicy(max_attempts=3, base_s=1, factor=1, jitter=0.0)
        rig = build_webhook_rig(uow_factory, policy=policy)
        rig.poster.default = status(503)
        order = await rig.new_order()

        for _ in range(3):
            await rig.drain()
            rig.clock.advance(seconds=1)
        assert await rig.sender.run_once() == 0

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.FAILED
        assert delivery.attempts == 3
        assert delivery.last_error == "gave up after 3 attempts: HTTP 503"
        outcomes = [a.outcome for a in await rig.attempts(delivery.event_id)]
        assert outcomes == [AttemptOutcome.RETRY, AttemptOutcome.RETRY, AttemptOutcome.FAILED]
        assert len(rig.poster.calls) == 3

    async def test_a_poster_that_blows_up_counts_as_a_failed_attempt_not_a_lost_delivery(
        self, rig: WebhookRig
    ) -> None:
        rig.poster.script.append(RuntimeError("bug"))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.PENDING
        assert delivery.last_error == "internal error (RuntimeError)"

    async def test_a_failed_delivery_can_get_a_fresh_round(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        policy = RetryPolicy(max_attempts=2, base_s=1, factor=1, jitter=0.0)
        rig = build_webhook_rig(uow_factory, policy=policy)
        rig.poster.default = status(503)
        order = await rig.new_order()
        for _ in range(2):
            await rig.drain()
            rig.clock.advance(seconds=1)
        (failed,) = await rig.deliveries(order)
        assert failed.status is D.FAILED

        async with uow_factory() as uow:
            assert await uow.webhooks.apply(failed, failed.requeued(now=rig.clock.now), None)
            await uow.commit()
        rig.poster.default = ok()
        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert (delivery.status, delivery.attempts, delivery.round_start) == (D.DELIVERED, 3, 2)
        numbers = [a.attempt_no for a in await rig.attempts(delivery.event_id)]
        assert numbers == [1, 2, 3]  # numbering continues; the history is never rewritten


class TestOrderingAndIsolation:
    async def test_an_orders_updates_go_out_in_order_each_after_the_last(
        self, rig: WebhookRig
    ) -> None:
        rig.poster.script.append(status(503))
        order = await rig.new_order()
        await rig.cancel(order)

        await rig.drain()
        assert [c.json["status"] for c in rig.poster.calls] == ["created"]  # cancelled waits

        rig.clock.advance(seconds=5)
        await rig.drain()

        assert [c.json["status"] for c in rig.poster.calls] == ["created", "created", "cancelled"]

    async def test_an_update_that_failed_for_good_does_not_block_the_next(
        self, rig: WebhookRig
    ) -> None:
        rig.poster.script.append(status(400))
        order = await rig.new_order()
        await rig.cancel(order)

        await rig.drain()

        first, second = await rig.deliveries(order)
        assert (first.status, second.status) == (D.FAILED, D.DELIVERED)

    async def test_one_orders_trouble_does_not_hold_up_another(self, rig: WebhookRig) -> None:
        rig.poster.default = ok()
        first = await rig.new_order("A")
        second = await rig.new_order("B")
        rig.poster.script.append(status(503))  # whichever goes first is refused

        await rig.drain()

        statuses = {d.status for d in [*await rig.deliveries(first), *await rig.deliveries(second)]}
        assert statuses == {D.PENDING, D.DELIVERED}

    async def test_a_partner_with_no_endpoint_is_skipped_without_a_request(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        rig = build_webhook_rig(uow_factory, endpoints={})
        order = await rig.new_order()

        assert await rig.drain() == 1

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.SKIPPED
        assert delivery.attempts == 0
        assert delivery.last_error is not None
        assert "no webhook endpoint" in delivery.last_error
        assert rig.poster.calls == []
        assert await rig.attempts(delivery.event_id) == []
        assert rig.sender.counts["skipped"] == 1

    async def test_each_partner_goes_to_its_own_endpoint(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        rig = build_webhook_rig(
            uow_factory,
            endpoints={
                "mock": WebhookEndpoint(url="https://a.example/hook"),
                "zomato": WebhookEndpoint(url="https://b.example/hook"),
            },
        )
        await rig.new_order("ORD-1", client_id="mock")
        await rig.new_order("99812", client_id="zomato")
        await rig.new_order("SW-1", client_id="swiggy")  # no endpoint

        await rig.drain()

        assert sorted(c.url for c in rig.poster.calls) == [
            "https://a.example/hook",
            "https://b.example/hook",
        ]
        assert rig.sender.counts["skipped"] == 1
        zomato = next(c for c in rig.poster.calls if c.url.startswith("https://b"))
        assert set(zomato.json) == {"order_id", "state", "event_ts", "event_ref"}  # its own format


class TestWhatCannotBeSent:
    async def test_a_partner_with_no_adapter_is_a_final_failure_without_a_request(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        rig = build_webhook_rig(uow_factory, registry=AdapterRegistry([]))
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.FAILED
        assert delivery.attempts == 0
        assert delivery.last_error is not None
        assert "no adapter registered" in delivery.last_error
        assert rig.poster.calls == []

    async def test_an_adapter_that_cannot_render_is_a_final_failure_without_a_request(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        class Broken:
            client_id = "mock"

            def render_status_update(self, order: Order, event: AuditEvent) -> Mapping[str, Any]:
                raise RuntimeError("bad template")

        rig = build_webhook_rig(uow_factory, registry=AdapterRegistry([Broken()]))  # type: ignore[list-item]
        order = await rig.new_order()

        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.FAILED
        assert delivery.last_error == "cannot render the update (RuntimeError)"
        assert rig.poster.calls == []


class TestSeveralSenders:
    async def test_a_claimed_delivery_is_left_alone_until_its_lease_runs_out(
        self, rig: WebhookRig, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await rig.new_order()
        now = rig.clock.now
        async with uow_factory() as uow:  # a sender takes it and then dies
            claimed = await uow.webhooks.claim_due(
                now=now, lease_until=now + timedelta(seconds=60), limit=10
            )
            await uow.commit()
        assert len(claimed) == 1

        assert await rig.sender.run_once() == 0
        rig.clock.advance(seconds=59)
        assert await rig.sender.run_once() == 0
        rig.clock.advance(seconds=1)
        assert await rig.sender.run_once() == 1  # sent after all: at-least-once

        (delivery,) = await rig.deliveries(order)
        assert delivery.status is D.DELIVERED

    async def test_a_sender_that_lost_its_lease_does_not_overwrite_the_one_that_took_over(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        clock = FakeClock()
        other = build_webhook_rig(uow_factory, clock=clock)

        class Interloper(FakePoster):
            """While this sender is mid-request its lease runs out and another sender delivers."""

            async def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> Any:
                clock.advance(seconds=61)
                await other.sender.run_once()
                return await super().post(url, body=body, headers=headers)

        slow = build_webhook_rig(uow_factory, clock=clock, poster=Interloper())
        order = await slow.new_order()

        assert await slow.sender.run_once() == 1

        assert slow.sender.counts["discarded"] == 1
        assert slow.poster.event_ids == other.poster.event_ids  # sent twice, same idempotency key
        (delivery,) = await slow.deliveries(order)
        assert (delivery.status, delivery.attempts) == (D.DELIVERED, 1)
        assert len(await slow.attempts(delivery.event_id)) == 1  # only the winner's record

    async def test_a_batch_is_sent_concurrently_up_to_the_limit(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        rig = build_webhook_rig(uow_factory, config=SenderConfig(concurrency=2))
        rig.poster.delay_s = 0.02
        for n in range(6):
            await rig.new_order(f"ORD-{n}")

        assert await rig.sender.run_once() == 6

        assert rig.poster.max_in_flight == 2
        assert {d.status for d in await rig.deliveries()} == {D.DELIVERED}

    async def test_a_pass_claims_no_more_than_the_batch_size(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        rig = build_webhook_rig(uow_factory, config=SenderConfig(batch_size=2))
        for n in range(5):
            await rig.new_order(f"ORD-{n}")

        assert await rig.sender.run_once() == 2
        assert await rig.drain() == 3


class TestTheLoop:
    async def test_it_sends_what_arrives_and_survives_a_failing_pass(
        self, uow_factory: UnitOfWorkFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = build_webhook_rig(uow_factory, config=SenderConfig(poll_s=0.01))
        real: Callable[[], Awaitable[int]] = rig.sender.run_once
        passes = {"n": 0}

        async def flaky() -> int:
            passes["n"] += 1
            if passes["n"] == 1:
                raise RuntimeError("database hiccup")
            return await real()

        monkeypatch.setattr(rig.sender, "run_once", flaky)
        task = asyncio.create_task(rig.sender.run())
        try:
            await rig.new_order()

            async def delivered() -> bool:
                return {d.status for d in await rig.deliveries()} == {D.DELIVERED}

            await until(delivered)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert passes["n"] >= 2
        (delivery,) = await rig.deliveries()
        assert delivery.status is D.DELIVERED
