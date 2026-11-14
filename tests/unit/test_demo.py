"""The one-command demo: the real platform on simulated drones, driven in virtual time."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.demo.__main__ import PAGE, add_demo_routes
from app.demo.demo import DOCKS, Demo, LoopbackMqtt, build_demo
from app.simulator.drone import Phase

ORDER = {
    "pickup": {"lat": 12.9716, "lng": 77.5946},
    "dropoff": {"lat": 12.9352, "lng": 77.6245},
    "package": {"weight_kg": 1.2, "description": "2 meals"},
}


@pytest.fixture
async def demo() -> AsyncIterator[Demo]:
    built = await build_demo(speed=60.0)
    add_demo_routes(built)
    async with built.app.router.lifespan_context(built.app):  # starts the drone link
        yield built


@pytest.fixture
async def http(demo: Demo) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=demo.app), base_url="http://demo.test"
    ) as client:
        yield client


async def run_until(demo: Demo, done: Callable[[], Awaitable[bool]], *, steps: int = 400) -> None:
    """Let the simulated world run (one virtual second per step) until ``done``."""
    for _ in range(steps):
        if await done():
            return
        await demo.step(1.0)
    raise AssertionError("the demo did not get there in time")


async def place(http: httpx.AsyncClient, external_id: str | None = None) -> tuple[str, str]:
    external_id = external_id or f"D-{uuid4().hex[:6]}"
    created = await http.post("/partner/mock/orders", json={"order_id": external_id, **ORDER})
    assert created.status_code == 201
    return external_id, created.json()["tracking_id"]


async def dispatch(http: httpx.AsyncClient, order_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()
    return body


async def status(http: httpx.AsyncClient, order_id: str) -> str:
    return str((await http.get(f"/internal/orders/{order_id}")).json()["status"])


class TestThePage:
    async def test_it_is_served_and_is_self_contained(self, http: httpx.AsyncClient) -> None:
        page = await http.get("/")

        assert page.status_code == 200
        assert "Drone delivery platform" in page.text
        assert "/ws/internal/fleet" in page.text
        assert "/partner/mock/orders" in page.text
        for external in ("https://", "//cdn", "<script src"):
            assert external not in page.text, "the page must work offline"

    async def test_the_map_data(self, http: httpx.AsyncClient) -> None:
        config = (await http.get("/demo/config")).json()

        assert set(config) == {"pickup", "dropoff", "docks"}
        assert sorted(d["name"] for d in config["docks"]) == sorted(DOCKS)
        assert config["pickup"] != config["dropoff"]

    def test_the_page_file_exists_next_to_the_code(self) -> None:
        assert PAGE.exists()
        assert PAGE.stat().st_size > 2000


class TestADeliveryOnTheRealPlatform:
    async def test_an_order_is_assigned_flown_delivered_and_the_drone_goes_home(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        _, order_id = await place(http)

        result = await dispatch(http, order_id)

        assert result["outcome"] == "started"
        assert result["order"]["status"] == "assigned"
        assert result["drone"]["name"] in DOCKS
        flown = UUID(result["drone"]["id"])
        assert demo.drones[flown].phase is Phase.FLYING

        async def delivered() -> bool:
            return await status(http, order_id) == "delivered"

        await run_until(demo, delivered)

        async def home() -> bool:
            return demo.drones[flown].phase is Phase.IDLE

        await run_until(demo, home)
        flights = (await http.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["completed"]
        assert (await http.get(f"/internal/drones/{flown}")).json()["status"] == "available"

    async def test_the_platform_recorded_every_step_in_its_audit_trail(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        _, order_id = await place(http)
        await dispatch(http, order_id)

        async def finished() -> bool:
            flights = (await http.get(f"/internal/orders/{order_id}/flights")).json()
            return bool(flights) and flights[0]["status"] == "completed"

        await run_until(demo, finished)

        trail = await demo.app.state.order_service.get_audit_trail(UUID(order_id))
        assert [e.new_state.value for e in trail] == [
            "created",
            "planned",
            "permission_granted",
            "mission_uploaded",
            "assigned",
            "started",
            "picked_up",
            "in_flight",
            "arriving",
            "delivered",
            "completed",
        ]
        assert {e.source.value for e in trail} >= {"adapter", "api", "telemetry"}

    async def test_the_drone_is_live_on_the_map_feed_while_it_flies(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        _, order_id = await place(http)
        result = await dispatch(http, order_id)
        flown = UUID(result["drone"]["id"])
        home = demo.drones[flown].home

        for _ in range(4):
            await demo.step(1.0)

        state = await demo.live.get_state(flown)
        assert state is not None
        assert (state.latitude, state.longitude) != (home.latitude, home.longitude)
        assert state.phase.value == "flying"
        assert state.battery_percent < 100.0


class TestTheRulesHoldInTheDemoToo:
    async def test_a_closed_airspace_means_no_flight_at_all(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        assert (await http.post("/demo/airspace", params={"closed": True})).json() == {
            "closed": True
        }
        _, order_id = await place(http)

        result = await dispatch(http, order_id)

        assert result["outcome"] == "permission_denied"
        assert "airspace closed" in result["detail"]
        assert all(d.phase is Phase.IDLE for d in demo.drones.values())  # nobody moved
        assert result["order"]["status"] == "created"  # and it can be tried again

    async def test_reopening_the_airspace_lets_the_same_order_fly(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        await http.post("/demo/airspace", params={"closed": True})
        _, order_id = await place(http)
        assert (await dispatch(http, order_id))["outcome"] == "permission_denied"

        await http.post("/demo/airspace", params={"closed": False})

        assert (await dispatch(http, order_id))["outcome"] == "started"

    async def test_cancelling_a_flying_order_recalls_the_drone_first(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        external_id, order_id = await place(http)
        flown = UUID((await dispatch(http, order_id))["drone"]["id"])
        await demo.step(2.0)

        cancelled = await http.post(f"/partner/mock/orders/{external_id}/cancel")

        assert cancelled.status_code == 200
        assert demo.drones[flown].phase is Phase.RETURNING  # the real recall reached the drone
        assert await status(http, order_id) == "cancelled"

        async def docked() -> bool:
            return demo.drones[flown].phase is Phase.IDLE

        await run_until(demo, docked)
        assert (await http.get(f"/internal/drones/{flown}")).json()["status"] == "available"

    async def test_two_orders_use_two_drones_and_a_third_waits(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        ids = [(await place(http))[1] for _ in range(3)]

        results = [await dispatch(http, order_id) for order_id in ids]

        assert [r["outcome"] for r in results] == ["started", "started", "no_drone_available"]
        assert results[0]["drone"]["id"] != results[1]["drone"]["id"]
        assert await status(http, ids[2]) == "created"  # waiting, not lost

    async def test_the_platform_never_receives_a_steering_command_only_the_five(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        _, order_id = await place(http)
        await dispatch(http, order_id)
        sent: list[str] = []
        for drone in demo.drones.values():
            original = drone.on_command

            def spy(command: Any, _original: Any = original) -> Any:
                sent.append(command.type)
                return _original(command)

            drone.on_command = spy  # type: ignore[method-assign]
        flight_id = (await http.get(f"/internal/orders/{order_id}/flights")).json()[0]["id"]

        await http.post(f"/internal/flights/{flight_id}/pause")
        await http.post(f"/internal/flights/{flight_id}/resume")
        await http.post(f"/internal/flights/{flight_id}/return-to-home")

        assert sent == ["pause_mission", "start_mission", "return_to_home"]


class TestTheLoopback:
    async def test_a_command_for_an_unknown_drone_is_not_sent(self) -> None:
        loopback = LoopbackMqtt()

        with pytest.raises(ConnectionError):
            await loopback.publish("drones/00000000-0000-0000-0000-000000000001/commands", b"{}")
