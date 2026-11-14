"""The one-command demo: the real platform on simulated drones, driven in virtual time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.demo.__main__ import PAGE, add_demo_routes
from app.demo.demo import DOCKS, Demo, LoopbackMqtt, MockPartner, build_demo
from app.simulator.drone import Phase
from app.webhooks.retry import RetryPolicy
from app.webhooks.signing import sign

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


# --- the partner's webhooks ---------------------------------------------------------------


@pytest.fixture
async def quick() -> AsyncIterator[Demo]:
    """A demo whose retries are quick, so a partner that was down catches up within the test."""
    built = await build_demo(
        speed=60.0, webhook_policy=RetryPolicy(max_attempts=50, base_s=0.02, factor=1, jitter=0.0)
    )
    add_demo_routes(built)
    async with built.app.router.lifespan_context(built.app):
        yield built
    await built.poster.aclose()


@pytest.fixture
async def quick_http(quick: Demo) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=quick.app), base_url="http://demo.test"
    ) as client:
        yield client


async def deliver_everything(demo: Demo, *, until_received: int | None = None) -> None:
    """Run the webhook sender until nothing is left to send (or the partner has ``n`` updates)."""
    async with asyncio.timeout(10):
        while True:
            await demo.sender.run_once()
            view = await demo.partner_view()
            settled = all(u["delivery"] == "delivered" for u in view["updates"])
            if (
                view["updates"]
                and settled
                and (until_received is None or view["received"] >= until_received)
            ):
                return
            await asyncio.sleep(0.03)


async def fly_to_the_end(demo: Demo, http: httpx.AsyncClient) -> str:
    _, order_id = await place(http)
    assert (await dispatch(http, order_id))["outcome"] == "started"

    async def delivered() -> bool:
        return await status(http, order_id) == "delivered"

    await run_until(demo, delivered)
    return order_id


JOURNEY = ["created", "assigned", "picked_up", "in_flight", "arriving", "delivered"]


class TestThePartnerHearsAboutIt:
    async def test_every_change_reaches_the_partner_in_order_and_signed(
        self, quick: Demo, quick_http: httpx.AsyncClient
    ) -> None:
        await fly_to_the_end(quick, quick_http)

        await deliver_everything(quick, until_received=6)

        assert [r["status"] for r in quick.partner.received] == JOURNEY
        assert all(r["signature_ok"] for r in quick.partner.received)
        assert [r["attempt"] for r in quick.partner.received] == [1] * 6
        assert quick.partner.duplicates == quick.partner.refused == 0

    async def test_the_page_data_shows_each_update_and_how_it_went(
        self, quick: Demo, quick_http: httpx.AsyncClient
    ) -> None:
        await fly_to_the_end(quick, quick_http)
        await deliver_everything(quick, until_received=6)

        view = (await quick_http.get("/demo/partner/view")).json()

        assert set(view) == {"down", "updates", "received", "refused", "duplicates"}
        assert [u["update"] for u in reversed(view["updates"])] == [
            s.replace("_", " ") for s in JOURNEY
        ]
        assert {u["delivery"] for u in view["updates"]} == {"delivered"}
        assert view["received"] == 6
        assert view["down"] is False

    async def test_a_partner_that_is_down_does_not_stop_a_delivery_and_catches_up_in_order(
        self, quick: Demo, quick_http: httpx.AsyncClient
    ) -> None:
        down = await quick_http.post("/demo/partner/down?down=true")
        assert down.json() == {"down": True}
        task = asyncio.create_task(quick.run_webhooks())
        try:
            order_id = await fly_to_the_end(quick, quick_http)  # the flight does not care

            async with asyncio.timeout(10):
                while (await quick.partner_view())["refused"] < 2:
                    await asyncio.sleep(0.03)
            waiting = await quick.partner_view()
            assert await status(quick_http, order_id) == "delivered"
            assert quick.partner.received == []
            assert {u["delivery"] for u in waiting["updates"]} == {"pending"}
            first = waiting["updates"][-1]  # the oldest: the order's creation
            assert first["attempts"] >= 1
            assert first["error"] == "HTTP 503"

            await quick_http.post("/demo/partner/down?down=false")
            async with asyncio.timeout(10):
                while len(quick.partner.received) < 6:
                    await asyncio.sleep(0.03)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert [r["status"] for r in quick.partner.received] == JOURNEY  # in order, once each
        assert quick.partner.duplicates == 0
        assert quick.partner.received[0]["attempt"] > 1  # the first update needed retries
        assert quick.partner.refused >= 2

    async def test_a_cancellation_is_told_too(
        self, quick: Demo, quick_http: httpx.AsyncClient
    ) -> None:
        external_id, _ = await place(quick_http)
        await quick_http.post(f"/partner/mock/orders/{external_id}/cancel")

        await deliver_everything(quick, until_received=2)

        assert [r["status"] for r in quick.partner.received] == ["created", "cancelled"]


class TestTheDemoPartner:
    def headers(
        self, body: bytes, *, secret: str = "demo-secret", event: str = "e-1"
    ) -> dict[str, str]:
        timestamp = 1_700_000_000
        return {
            "x-event-id": event,
            "x-delivery-attempt": "1",
            "x-timestamp": str(timestamp),
            "x-signature": sign(secret, timestamp, body),
        }

    BODY = b'{"order_id":"O-1","status":"created","event_id":"e-1","occurred_at":"x"}'

    def test_it_accepts_a_signed_update_once_and_ignores_a_repeat(self) -> None:
        partner = MockPartner()

        first = partner.handle(body=self.BODY, headers=self.headers(self.BODY))
        again = partner.handle(body=self.BODY, headers=self.headers(self.BODY))

        assert (first, again) == (204, 200)
        assert len(partner.received) == 1
        assert partner.duplicates == 1
        assert partner.received[0]["signature_ok"] is True

    def test_a_bad_signature_is_noticed(self) -> None:
        partner = MockPartner()

        partner.handle(body=self.BODY, headers=self.headers(self.BODY, secret="someone else"))

        assert partner.received[0]["signature_ok"] is False

    @pytest.mark.parametrize("body", [b"not json", b""])
    def test_a_body_that_is_not_json_is_a_400(self, body: bytes) -> None:
        assert MockPartner().handle(body=body, headers=self.headers(body)) == 400

    def test_missing_headers_are_a_400(self) -> None:
        assert MockPartner().handle(body=self.BODY, headers={}) == 400

    def test_when_it_is_down_it_answers_503_and_keeps_nothing(self) -> None:
        partner = MockPartner(down=True)

        code = partner.handle(body=self.BODY, headers=self.headers(self.BODY))

        assert code == 503
        assert (partner.received, partner.refused) == ([], 1)


class TestTheWebhookRoutes:
    async def test_the_partner_routes_are_not_part_of_the_platform_api(
        self, http: httpx.AsyncClient
    ) -> None:
        paths = (await http.get("/openapi.json")).json()["paths"]

        assert not [p for p in paths if p.startswith("/demo")]

    async def test_the_receiver_answers_what_the_partner_would(
        self, demo: Demo, http: httpx.AsyncClient
    ) -> None:
        body = TestTheDemoPartner.BODY
        headers = TestTheDemoPartner().headers(body)

        accepted = await http.post("/demo/partner/webhook", content=body, headers=headers)
        down = await http.post("/demo/partner/down?down=true")
        refused = await http.post("/demo/partner/webhook", content=body, headers=headers)

        assert accepted.status_code == 204
        assert down.json() == {"down": True}
        assert refused.status_code == 503

    async def test_the_page_has_the_panel(self, http: httpx.AsyncClient) -> None:
        page = (await http.get("/")).text

        for needle in (
            "Partner webhooks",
            "partnerdown",
            "/demo/partner/view",
            "/demo/partner/down",
        ):
            assert needle in page
