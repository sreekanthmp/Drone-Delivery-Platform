"""Webhooks end to end on PostgreSQL: the real API, the real sender, a real partner over HTTP.

A partner order goes in through the API; the sender, built the way the process builds it, POSTs
each status change to a real HTTP server standing in for the partner, which verifies the
signature and can be told to fail. Everything an operator would look at is read back through the
internal API.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.config import Settings
from app.domain.models import DeliveryStatus
from app.main import create_app
from app.services.ports import UnitOfWorkFactory
from app.webhook_worker import build_sender
from app.webhooks.http import HttpxPoster
from app.webhooks.sender import WebhookSender
from app.webhooks.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from tests.api.live_server import serve
from tests.payloads import EXTERNAL_IDS, VALID_PAYLOADS

pytestmark = pytest.mark.integration

SECRET = "e2e-secret"


@dataclass
class Partner:
    """A partner's server: records what it is sent, and answers from a script."""

    base: str = ""
    received: list[dict[str, Any]] = field(default_factory=list)
    answers: list[int] = field(default_factory=list)  # status codes to answer with first

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/hook")
        async def hook(request: Request) -> Response:
            body = await request.body()
            headers = dict(request.headers)
            code = self.answers.pop(0) if self.answers else 204
            self.received.append(
                {
                    "body": body,
                    "headers": headers,
                    "code": code,
                    "signed_ok": verify(
                        SECRET,
                        int(headers[TIMESTAMP_HEADER.lower()]),
                        body,
                        headers[SIGNATURE_HEADER.lower()],
                    ),
                }
            )
            return Response(status_code=code)

        return app

    @property
    def event_ids(self) -> list[str]:
        return [r["headers"]["x-event-id"] for r in self.received]

    @property
    def statuses(self) -> list[str]:
        return [json.loads(r["body"])["status"] for r in self.received if r["code"] < 300]


@pytest.fixture
async def partner() -> AsyncIterator[Partner]:
    partner = Partner()
    async with serve(partner.app()) as host:
        partner.base = f"http://{host}"
        yield partner


@pytest.fixture
async def platform(pg_uow_factory: UnitOfWorkFactory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=Settings(_env_file=None), uow_factory=pg_uow_factory)  # type: ignore[call-arg]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as http:
        yield http


@pytest.fixture
async def sender(
    pg_uow_factory: UnitOfWorkFactory, partner: Partner
) -> AsyncIterator[WebhookSender]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        webhook_endpoints={"mock": {"url": f"{partner.base}/hook", "secret": SECRET}},
        webhook_max_attempts=3,
        webhook_backoff_base_seconds=0.05,
        webhook_backoff_factor=1,
        webhook_backoff_jitter=0,
        webhook_timeout_seconds=2,
        webhook_connect_timeout_seconds=1,
        webhook_lease_seconds=30,
    )
    poster = HttpxPoster(timeout_s=2, connect_timeout_s=1)
    try:
        yield build_sender(settings, pg_uow_factory, poster)
    finally:
        await poster.aclose()


async def place(platform: httpx.AsyncClient) -> str:
    response = await platform.post("/partner/mock/orders", json=VALID_PAYLOADS["mock"]())
    assert response.status_code == 201, response.text
    return str(response.json()["tracking_id"])


async def cancel(platform: httpx.AsyncClient) -> None:
    response = await platform.post(f"/partner/mock/orders/{EXTERNAL_IDS['mock']}/cancel")
    assert response.status_code == 200, response.text


async def settle(sender: WebhookSender, platform: httpx.AsyncClient, timeout: float = 10) -> None:
    """Run the sender until nothing is pending any more."""
    async with asyncio.timeout(timeout):
        while True:
            await sender.run_once()
            pending = (await platform.get("/internal/webhooks/deliveries?status=pending")).json()
            if not pending:
                return
            await asyncio.sleep(0.05)


async def deliveries(platform: httpx.AsyncClient, order_id: str) -> list[dict[str, Any]]:
    response = await platform.get(f"/internal/orders/{order_id}/webhooks")
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    return body


async def test_a_partner_is_told_of_every_change_in_order_and_properly_signed(
    platform: httpx.AsyncClient, sender: WebhookSender, partner: Partner
) -> None:
    order_id = await place(platform)
    await cancel(platform)

    await settle(sender, platform)

    assert partner.statuses == ["created", "cancelled"]
    assert all(r["signed_ok"] for r in partner.received)
    assert all(r["headers"]["x-delivery-attempt"] == "1" for r in partner.received)
    assert all(r["headers"]["content-type"] == "application/json" for r in partner.received)
    mine = await deliveries(platform, order_id)
    assert [d["status"] for d in mine] == ["delivered", "delivered"]
    assert partner.event_ids == [d["event_id"] for d in mine]  # the audit events' own ids


async def test_a_partner_that_is_briefly_down_is_retried_and_catches_up_in_order(
    platform: httpx.AsyncClient, sender: WebhookSender, partner: Partner
) -> None:
    partner.answers = [503, 503]
    order_id = await place(platform)
    await cancel(platform)

    await settle(sender, platform)

    # The first update took three attempts; the second waited behind it, then went out.
    assert partner.statuses == ["created", "cancelled"]
    first, second = await deliveries(platform, order_id)
    assert (first["status"], first["attempts"]) == ("delivered", 3)
    assert (second["status"], second["attempts"]) == ("delivered", 1)
    assert partner.event_ids == [first["event_id"]] * 3 + [second["event_id"]]
    detail = (await platform.get(f"/internal/webhooks/deliveries/{first['event_id']}")).json()
    assert [a["outcome"] for a in detail["attempts"]] == ["retry", "retry", "delivered"]
    assert [a["status_code"] for a in detail["attempts"]] == [503, 503, 204]
    assert [r["headers"]["x-delivery-attempt"] for r in partner.received[:3]] == ["1", "2", "3"]


async def test_a_partner_that_rejects_it_is_recorded_and_can_be_given_another_go(
    platform: httpx.AsyncClient, sender: WebhookSender, partner: Partner
) -> None:
    partner.answers = [400]
    order_id = await place(platform)

    await settle(sender, platform)

    (failed,) = await deliveries(platform, order_id)
    assert (failed["status"], failed["attempts"], failed["last_status_code"]) == ("failed", 1, 400)
    assert failed["last_error"] == "the partner rejected it: HTTP 400"
    assert len(partner.received) == 1  # no retries of a rejection

    retried = await platform.post(f"/internal/webhooks/deliveries/{failed['event_id']}/retry")
    assert retried.status_code == 200
    await settle(sender, platform)

    (done,) = await deliveries(platform, order_id)
    assert (done["status"], done["attempts"]) == ("delivered", 2)
    assert partner.event_ids == [failed["event_id"]] * 2  # the same idempotency key both times
    detail = (await platform.get(f"/internal/webhooks/deliveries/{failed['event_id']}")).json()
    assert [(a["attempt_no"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "failed"),
        (2, "delivered"),
    ]


async def test_a_partner_that_stays_down_is_given_up_on_after_the_attempts_run_out(
    platform: httpx.AsyncClient, sender: WebhookSender, partner: Partner
) -> None:
    partner.answers = [500] * 10
    order_id = await place(platform)

    await settle(sender, platform)

    (failed,) = await deliveries(platform, order_id)
    assert (failed["status"], failed["attempts"]) == ("failed", 3)
    assert failed["last_error"] == "gave up after 3 attempts: HTTP 500"
    assert len(partner.received) == 3
    listing = (await platform.get("/internal/webhooks/deliveries?status=failed")).json()
    assert [d["event_id"] for d in listing] == [failed["event_id"]]


async def test_a_partner_with_no_endpoint_is_not_told_and_nothing_is_sent(
    platform: httpx.AsyncClient, sender: WebhookSender, partner: Partner
) -> None:
    response = await platform.post("/partner/zomato/orders", json=VALID_PAYLOADS["zomato"]())
    assert response.status_code == 201

    await settle(sender, platform)

    (skipped,) = (await platform.get("/internal/webhooks/deliveries")).json()
    assert (skipped["client_id"], skipped["status"], skipped["attempts"]) == (
        "zomato",
        "skipped",
        0,
    )
    assert partner.received == []


async def test_the_database_holds_exactly_what_the_api_reports(
    platform: httpx.AsyncClient,
    sender: WebhookSender,
    partner: Partner,
    pg_uow_factory: UnitOfWorkFactory,
) -> None:
    partner.answers = [503]
    order_id = await place(platform)
    await settle(sender, platform)

    async with pg_uow_factory() as uow:
        stored = await uow.webhooks.list_for_order(UUID(order_id))
        attempts = await uow.webhooks.attempts_for(stored[0].event_id)

    assert [d.status for d in stored] == [DeliveryStatus.DELIVERED]
    assert [(a.attempt_no, a.outcome.value) for a in attempts] == [(1, "retry"), (2, "delivered")]
