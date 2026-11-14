"""Partner order cancellation, through each client's adapter (Rules 3 and 1).

A partner cancels by its own order id and always gets its own response format. If the drone
is flying it is recalled first; if the recall cannot be confirmed the order is NOT cancelled.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.dispatch.link import NoAcknowledgementError
from app.domain.models import EventSource, OrderStatus
from app.services.order_service import OrderService
from tests.api.conftest import (
    CLIENTS,
    PARTNER_STATUS,
    PLATFORM_REF,
    lookup_path,
    place_order,
    register_drone,
)
from tests.dispatch_rig import HookedLink
from tests.payloads import EXTERNAL_IDS

CANCELLED_NAME = {"mock": "cancelled", "swiggy": "CANCELLED", "zomato": "cancelled"}
ERROR_KIND: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda b: b["error"]["code"],
    "swiggy": lambda b: b["error_code"].lower(),
    "zomato": lambda b: b["error"]["type"],
}


def cancel_path(client_id: str, external_id: str | None = None) -> str:
    return f"/partner/{client_id}/orders/{external_id or EXTERNAL_IDS[client_id]}/cancel"


async def internal_order(client: httpx.AsyncClient, order_id: UUID) -> dict[str, Any]:
    body: dict[str, Any] = (await client.get(f"/internal/orders/{order_id}")).json()
    return body


@pytest.mark.parametrize("client_id", CLIENTS)
class TestCancel:
    async def test_a_new_order_is_cancelled_in_the_partners_format(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        order_id = await place_order(client, client_id)

        response = await client.post(cancel_path(client_id))

        assert response.status_code == 200
        body = response.json()
        assert PARTNER_STATUS[client_id](body) == CANCELLED_NAME[client_id]
        assert PLATFORM_REF[client_id](body) == str(order_id)
        assert (await internal_order(client, order_id))["status"] == "cancelled"

    async def test_the_lookup_then_shows_the_cancellation(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        await place_order(client, client_id)
        await client.post(cancel_path(client_id))

        response = await client.get(lookup_path(client_id))

        assert PARTNER_STATUS[client_id](response.json()) == CANCELLED_NAME[client_id]

    async def test_cancelling_twice_is_idempotent(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        await place_order(client, client_id)
        first = await client.post(cancel_path(client_id))

        second = await client.post(cancel_path(client_id))

        assert second.status_code == 200
        assert second.json() == first.json()

    async def test_an_unknown_order_is_not_found_in_the_partners_format(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        response = await client.post(cancel_path(client_id, "NO-SUCH-ORDER"))

        assert response.status_code == 404
        assert ERROR_KIND[client_id](response.json()) == "not_found"

    async def test_an_order_that_has_already_failed_is_a_conflict(
        self, client: httpx.AsyncClient, service: OrderService, client_id: str
    ) -> None:
        order_id = await place_order(client, client_id)
        await service.transition(order_id, OrderStatus.FAILED, source=EventSource.SYSTEM)

        response = await client.post(cancel_path(client_id))

        assert response.status_code == 409
        assert ERROR_KIND[client_id](response.json()) == "conflict"
        assert (await internal_order(client, order_id))["status"] == "failed"

    async def test_a_staged_flight_is_cancelled_and_the_drone_released(
        self, client: httpx.AsyncClient, link: HookedLink, client_id: str
    ) -> None:
        drone = await register_drone(client)
        order_id = await place_order(client, client_id)
        await client.post(f"/internal/orders/{order_id}/dispatch", params={"start": False})

        response = await client.post(cancel_path(client_id))

        assert response.status_code == 200
        assert (await internal_order(client, order_id))["status"] == "cancelled"
        flights = (await client.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["cancelled"]
        assert (await client.get(f"/internal/drones/{drone['id']}")).json()["status"] == "available"
        assert [op for op, _ in link.commands] == ["upload"]  # nothing was flying

    async def test_a_flying_drone_is_recalled_first(
        self, client: httpx.AsyncClient, link: HookedLink, client_id: str
    ) -> None:
        drone = await register_drone(client)
        order_id = await place_order(client, client_id)
        await client.post(f"/internal/orders/{order_id}/dispatch")

        response = await client.post(cancel_path(client_id))

        assert response.status_code == 200
        assert PARTNER_STATUS[client_id](response.json()) == CANCELLED_NAME[client_id]
        assert [op for op, _ in link.commands] == ["upload", "start", "return_to_home"]
        flights = (await client.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["aborted"]
        assert (await client.get(f"/internal/drones/{drone['id']}")).json()["status"] == (
            "returning"
        )

    async def test_if_the_drone_cannot_be_recalled_the_order_is_not_cancelled(
        self, client: httpx.AsyncClient, link: HookedLink, client_id: str
    ) -> None:
        await register_drone(client)
        order_id = await place_order(client, client_id)
        await client.post(f"/internal/orders/{order_id}/dispatch")
        link.set_failure("return_to_home", NoAcknowledgementError("no answer"))

        response = await client.post(cancel_path(client_id))

        assert response.status_code == 503
        assert ERROR_KIND[client_id](response.json()) == "unavailable"
        assert (await internal_order(client, order_id))["status"] == "assigned"
        flights = (await client.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["started"]  # still flying: still a live order

        link.set_failure("return_to_home", None)  # and the retry works
        assert (await client.post(cancel_path(client_id))).status_code == 200
        assert (await internal_order(client, order_id))["status"] == "cancelled"


class TestCancelRouting:
    async def test_an_unknown_client(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/partner/nobody/orders/X/cancel")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "unknown_client"

    async def test_one_partner_cannot_cancel_anothers_order(
        self, client: httpx.AsyncClient
    ) -> None:
        mock_order = await place_order(client, "mock")

        # the swiggy id of the same string does not exist for swiggy
        response = await client.post(cancel_path("swiggy", EXTERNAL_IDS["mock"]))

        assert response.status_code == 404
        assert (await internal_order(client, mock_order))["status"] == "created"

    async def test_get_is_not_allowed_on_the_cancel_route(self, client: httpx.AsyncClient) -> None:
        response = await client.get(cancel_path("mock"))

        assert response.status_code == 405
