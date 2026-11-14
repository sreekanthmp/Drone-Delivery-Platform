"""Internal fleet endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from tests.api.conftest import DRONE_BODY, place_order, register_drone

MISSING = "00000000-0000-0000-0000-0000000000ff"


class TestRegister:
    async def test_a_new_drone_is_available_at_its_dock(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/internal/drones", json=DRONE_BODY)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "available"
        assert body["name"] == "drone-1"
        assert body["home"] == {"latitude": 12.98, "longitude": 77.6}
        assert body["max_payload_kg"] == 5.0
        assert body["max_range_km"] == 20.0
        assert body["battery_percent"] == 90.0
        UUID(body["id"])

    async def test_a_status_cannot_be_supplied(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/internal/drones", json={**DRONE_BODY, "status": "on_mission"}
        )

        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "  "),
            ("max_payload_kg", 0),
            ("max_payload_kg", -1),
            ("max_range_km", 0),
            ("battery_percent", 101),
            ("battery_percent", -1),
            ("home", {"latitude": 95.0, "longitude": 0.0}),
        ],
    )
    async def test_an_invalid_drone_is_rejected_in_the_platform_format(
        self, client: httpx.AsyncClient, field: str, value: Any
    ) -> None:
        response = await client.post("/internal/drones", json={**DRONE_BODY, field: value})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    @pytest.mark.parametrize("field", list(DRONE_BODY))
    async def test_every_field_is_required(self, client: httpx.AsyncClient, field: str) -> None:
        body = {k: v for k, v in DRONE_BODY.items() if k != field}

        response = await client.post("/internal/drones", json=body)

        assert response.status_code == 422

    async def test_a_duplicate_name_is_a_conflict(self, client: httpx.AsyncClient) -> None:
        await register_drone(client)

        response = await client.post("/internal/drones", json=DRONE_BODY)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"


class TestRead:
    async def test_list_and_get(self, client: httpx.AsyncClient) -> None:
        a = await register_drone(client, name="a")
        b = await register_drone(client, name="b")

        listed = (await client.get("/internal/drones")).json()
        fetched = (await client.get(f"/internal/drones/{a['id']}")).json()

        assert [d["name"] for d in listed] == ["a", "b"]
        assert fetched == a
        assert {d["id"] for d in listed} == {a["id"], b["id"]}

    async def test_an_unknown_drone_is_not_found(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"/internal/drones/{MISSING}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_there_is_no_status_filter_or_status_write(
        self, client: httpx.AsyncClient
    ) -> None:
        drone = await register_drone(client)

        for method in ("put", "patch", "delete"):
            response = await client.request(method, f"/internal/drones/{drone['id']}")
            assert response.status_code == 405


class TestOperationalCommands:
    async def test_maintenance_then_docked(self, client: httpx.AsyncClient) -> None:
        drone = await register_drone(client)

        maintenance = await client.post(f"/internal/drones/{drone['id']}/maintenance")
        assert maintenance.status_code == 200
        assert maintenance.json()["status"] == "maintenance"

        docked = await client.post(f"/internal/drones/{drone['id']}/docked")
        assert docked.status_code == 200
        assert docked.json()["status"] == "available"

    @pytest.mark.parametrize("command", ["docked", "maintenance"])
    async def test_an_unknown_drone_is_not_found(
        self, client: httpx.AsyncClient, command: str
    ) -> None:
        response = await client.post(f"/internal/drones/{MISSING}/{command}")

        assert response.status_code == 404

    @pytest.mark.parametrize("command", ["docked", "maintenance"])
    async def test_a_drone_with_a_live_flight_is_a_conflict(
        self, client: httpx.AsyncClient, command: str
    ) -> None:
        drone = await register_drone(client)
        order_id = await place_order(client)
        dispatched = await client.post(f"/internal/orders/{order_id}/dispatch")
        assert dispatched.json()["outcome"] == "started"

        response = await client.post(f"/internal/drones/{drone['id']}/{command}")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    async def test_an_illegal_status_change_is_a_conflict(self, client: httpx.AsyncClient) -> None:
        drone = await register_drone(client)
        order_id = await place_order(client)
        started = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()
        await client.post(f"/internal/flights/{started['flight']['id']}/return-to-home")

        # a recalled drone is 'returning', and 'returning -> maintenance' is not an allowed edge
        response = await client.post(f"/internal/drones/{drone['id']}/maintenance")

        assert response.status_code == 409
