"""Internal order endpoints: platform-format, read-only."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx

from app.domain.models import EventSource, OrderStatus
from app.services.order_service import OrderService
from tests.api.conftest import PLATFORM_REF, create_path
from tests.payloads import VALID_PAYLOADS

S = OrderStatus
DRONE = UUID(int=0xD0D0)


async def _create(client: httpx.AsyncClient, client_id: str = "swiggy") -> UUID:
    response = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())
    return UUID(PLATFORM_REF[client_id](response.json()))


class TestGetOrder:
    async def test_returns_the_order_in_the_platform_format(
        self, client: httpx.AsyncClient
    ) -> None:
        order_id = await _create(client, "swiggy")

        response = await client.get(f"/internal/orders/{order_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(order_id)
        assert body["client_id"] == "swiggy"
        assert body["external_order_id"] == "SW-100234"
        assert body["status"] == "created"
        assert body["drone_id"] is None
        assert body["pickup"] == {"latitude": 12.9716, "longitude": 77.5946}
        assert body["dropoff"] == {"latitude": 12.9352, "longitude": 77.6245}
        assert body["package"]["weight_kg"] == 1.1
        assert body["package"]["description"] == "2 x Biryani, 1 x Raita"
        assert set(body) == {
            "id",
            "client_id",
            "external_order_id",
            "status",
            "pickup",
            "dropoff",
            "package",
            "drone_id",
            "created_at",
            "updated_at",
        }

    async def test_shows_the_drone_once_assigned(
        self, client: httpx.AsyncClient, service: OrderService
    ) -> None:
        order_id = await _create(client)
        await service.transition(order_id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=DRONE)

        body = (await client.get(f"/internal/orders/{order_id}")).json()

        assert body["status"] == "assigned"
        assert body["drone_id"] == str(DRONE)

    async def test_an_unknown_order_is_a_neutral_404(self, client: httpx.AsyncClient) -> None:
        missing = uuid4()

        response = await client.get(f"/internal/orders/{missing}")

        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "not_found", "message": f"Order {missing} not found"}
        }

    async def test_a_malformed_id_is_a_validation_error(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/internal/orders/not-a-uuid")

        assert response.status_code == 422


class TestGetStatus:
    async def test_returns_status_and_terminal_flag(
        self, client: httpx.AsyncClient, service: OrderService
    ) -> None:
        order_id = await _create(client)

        before = (await client.get(f"/internal/orders/{order_id}/status")).json()
        await service.transition(order_id, S.CANCELLED, source=EventSource.OPS)
        after = (await client.get(f"/internal/orders/{order_id}/status")).json()

        assert before["status"] == "created"
        assert before["is_terminal"] is False
        assert after["status"] == "cancelled"
        assert after["is_terminal"] is True
        assert after["order_id"] == str(order_id)
        assert set(after) == {"order_id", "status", "is_terminal", "updated_at"}

    async def test_an_unknown_order_is_a_neutral_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/internal/orders/{uuid4()}/status")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestInternalApiIsReadOnly:
    async def test_other_methods_are_not_allowed(self, client: httpx.AsyncClient) -> None:
        order_id = await _create(client)
        path = f"/internal/orders/{order_id}"

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await client.request(method, path, json={"status": "delivered"})
            assert response.status_code == 405, method
            response = await client.request(method, f"{path}/status", json={"status": "delivered"})
            assert response.status_code == 405, method

        assert (await client.get(f"{path}/status")).json()["status"] == "created"
