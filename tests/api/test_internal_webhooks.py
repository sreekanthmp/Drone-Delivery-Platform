"""Internal webhook endpoints: how notifications to partners are going, and the one retry verb."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.webhooks.retry import RetryPolicy
from tests.api.conftest import place_order
from tests.conftest import FakeClock
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork
from tests.payloads import EXTERNAL_IDS
from tests.webhook_rig import WebhookRig, build_webhook_rig, status


@pytest.fixture
def sender(database: InMemoryDatabase) -> WebhookRig:
    """A sender on the app's own database, on a clock that is already past every event."""
    clock = FakeClock(datetime.now(UTC) + timedelta(hours=1))
    return build_webhook_rig(
        lambda: InMemoryUnitOfWork(database),
        clock=clock,
        policy=RetryPolicy(max_attempts=2, base_s=1, factor=1, jitter=0.0),
    )


async def cancel(client: httpx.AsyncClient, client_id: str = "mock") -> None:
    response = await client.post(f"/partner/{client_id}/orders/{EXTERNAL_IDS[client_id]}/cancel")
    assert response.status_code == 200, response.text


class TestListing:
    async def test_it_starts_empty(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/internal/webhooks/deliveries")

        assert response.status_code == 200
        assert response.json() == []

    async def test_an_order_queues_a_notification_the_partner_has_not_had_yet(
        self, client: httpx.AsyncClient
    ) -> None:
        order_id = await place_order(client)

        (delivery,) = (await client.get("/internal/webhooks/deliveries")).json()

        assert delivery["order_id"] == str(order_id)
        assert delivery["client_id"] == "mock"
        assert delivery["status"] == "pending"
        assert delivery["attempts"] == 0
        assert delivery["last_status_code"] is None
        assert delivery["last_error"] is None
        assert delivery["delivered_at"] is None
        assert set(delivery) == {
            "event_id",
            "order_id",
            "client_id",
            "status",
            "attempts",
            "next_attempt_at",
            "last_status_code",
            "last_error",
            "delivered_at",
            "created_at",
            "updated_at",
        }

    async def test_it_is_newest_first_and_filterable(
        self, client: httpx.AsyncClient, sender: WebhookRig
    ) -> None:
        await place_order(client)
        await cancel(client)
        await sender.drain()
        await place_order(client, "zomato")

        everything = (await client.get("/internal/webhooks/deliveries")).json()
        assert [d["status"] for d in everything] == ["pending", "delivered", "delivered"]
        assert [d["client_id"] for d in everything] == ["zomato", "mock", "mock"]

        pending = (await client.get("/internal/webhooks/deliveries?status=pending")).json()
        assert [d["client_id"] for d in pending] == ["zomato"]
        mock = (await client.get("/internal/webhooks/deliveries?client_id=mock")).json()
        assert len(mock) == 2
        one = (await client.get("/internal/webhooks/deliveries?limit=1")).json()
        assert len(one) == 1
        none = (await client.get("/internal/webhooks/deliveries?status=failed")).json()
        assert none == []

    @pytest.mark.parametrize(
        "query", ["status=bogus", "limit=0", "limit=501", "limit=abc", "status=DELIVERED"]
    )
    async def test_a_bad_query_is_a_422(self, client: httpx.AsyncClient, query: str) -> None:
        response = await client.get(f"/internal/webhooks/deliveries?{query}")

        assert response.status_code == 422

    async def test_an_orders_deliveries_are_listed_oldest_first(
        self, client: httpx.AsyncClient
    ) -> None:
        order_id = await place_order(client)
        await cancel(client)

        response = await client.get(f"/internal/orders/{order_id}/webhooks")

        assert response.status_code == 200
        deliveries = response.json()
        assert len(deliveries) == 2
        assert {d["order_id"] for d in deliveries} == {str(order_id)}
        assert deliveries[0]["created_at"] <= deliveries[1]["created_at"]

    async def test_an_unknown_order_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/internal/orders/{uuid4()}/webhooks")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_a_malformed_id_is_a_422(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/internal/orders/nope/webhooks")).status_code == 422
        assert (await client.get("/internal/webhooks/deliveries/nope")).status_code == 422


class TestOneDelivery:
    async def test_it_shows_every_attempt(
        self, client: httpx.AsyncClient, sender: WebhookRig
    ) -> None:
        sender.poster.script.append(status(503))
        await place_order(client)
        await sender.drain()
        sender.clock.advance(seconds=1)
        await sender.drain()
        (delivery,) = (await client.get("/internal/webhooks/deliveries")).json()

        response = await client.get(f"/internal/webhooks/deliveries/{delivery['event_id']}")

        assert response.status_code == 200
        body = response.json()
        assert body["delivery"]["status"] == "delivered"
        assert body["delivery"]["attempts"] == 2
        assert [(a["attempt_no"], a["outcome"], a["status_code"]) for a in body["attempts"]] == [
            (1, "retry", 503),
            (2, "delivered", 200),
        ]
        assert body["attempts"][0]["error"] == "HTTP 503"
        assert set(body["attempts"][0]) == {
            "attempt_no",
            "started_at",
            "duration_ms",
            "outcome",
            "status_code",
            "error",
        }

    async def test_a_delivery_nobody_tried_yet_has_no_attempts(
        self, client: httpx.AsyncClient
    ) -> None:
        await place_order(client)
        (delivery,) = (await client.get("/internal/webhooks/deliveries")).json()

        body = (await client.get(f"/internal/webhooks/deliveries/{delivery['event_id']}")).json()

        assert body["attempts"] == []

    async def test_an_unknown_one_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/internal/webhooks/deliveries/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestRetry:
    async def failed(self, client: httpx.AsyncClient, sender: WebhookRig) -> str:
        sender.poster.script.append(status(404))
        await place_order(client)
        await sender.drain()
        (delivery,) = (await client.get("/internal/webhooks/deliveries?status=failed")).json()
        return str(delivery["event_id"])

    async def test_a_failed_delivery_goes_round_again(
        self, client: httpx.AsyncClient, sender: WebhookRig
    ) -> None:
        event_id = await self.failed(client, sender)

        response = await client.post(f"/internal/webhooks/deliveries/{event_id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert response.json()["event_id"] == event_id
        await sender.drain()
        body = (await client.get(f"/internal/webhooks/deliveries/{event_id}")).json()
        assert body["delivery"]["status"] == "delivered"
        assert [a["outcome"] for a in body["attempts"]] == ["failed", "delivered"]

    @pytest.mark.parametrize("already", ["pending", "delivered"])
    async def test_anything_but_a_failed_delivery_is_a_409(
        self, client: httpx.AsyncClient, sender: WebhookRig, already: str
    ) -> None:
        await place_order(client)
        if already == "delivered":
            await sender.drain()
        (delivery,) = (await client.get("/internal/webhooks/deliveries")).json()

        response = await client.post(f"/internal/webhooks/deliveries/{delivery['event_id']}/retry")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        assert f"is {already}" in response.json()["error"]["message"]

    async def test_asking_twice_is_a_409_the_second_time(
        self, client: httpx.AsyncClient, sender: WebhookRig
    ) -> None:
        event_id = await self.failed(client, sender)
        assert (
            await client.post(f"/internal/webhooks/deliveries/{event_id}/retry")
        ).status_code == 200

        again = await client.post(f"/internal/webhooks/deliveries/{event_id}/retry")

        assert again.status_code == 409

    async def test_an_unknown_one_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/internal/webhooks/deliveries/{uuid4()}/retry")

        assert response.status_code == 404

    async def test_retrying_changes_no_order(
        self, client: httpx.AsyncClient, sender: WebhookRig, database: InMemoryDatabase
    ) -> None:
        event_id = await self.failed(client, sender)
        before = (list(database.orders.values()), len(database.audit))

        await client.post(f"/internal/webhooks/deliveries/{event_id}/retry")

        assert (list(database.orders.values()), len(database.audit)) == before


class TestWhatIsNeverShown:
    async def test_no_response_carries_an_endpoint_a_secret_or_a_request_body(
        self, client: httpx.AsyncClient, sender: WebhookRig
    ) -> None:
        sender.poster.script.append(status(503))
        order_id = await place_order(client)
        await sender.drain()
        (delivery,) = (await client.get("/internal/webhooks/deliveries")).json()
        event_id = delivery["event_id"]

        texts = [
            (await client.get("/internal/webhooks/deliveries")).text,
            (await client.get(f"/internal/webhooks/deliveries/{event_id}")).text,
            (await client.get(f"/internal/orders/{order_id}/webhooks")).text,
        ]

        for text in texts:
            for word in (
                "partner.example",
                "secret",
                "signature",
                "url",
                "body",
                "drone",
                "flight",
            ):
                assert word not in text.lower(), word


def test_nothing_in_the_responses_is_a_raw_dict_of_internals(app: Any) -> None:
    schema = app.openapi()["components"]["schemas"]

    assert set(schema["DeliveryOut"]["properties"]) >= {"event_id", "status", "attempts"}
    assert "url" not in schema["DeliveryOut"]["properties"]
    assert "secret" not in schema["DeliveryOut"]["properties"]
