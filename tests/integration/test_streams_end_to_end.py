"""The live streams on the whole real stack.

Simulated drones fly through a real Mosquitto; the worker records their samples into a real
Redis; the API (a real uvicorn socket) streams them to real WebSocket clients. Nothing is faked
between a drone's sample and the bytes a watcher receives.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.mqtt.client import MqttClient
from app.realtime.connection import connect as connect_redis
from app.realtime.redis_backend import RedisRealtime
from app.services.ports import UnitOfWorkFactory
from tests.api.conftest import place_order
from tests.api.live_server import close_code, connect, frame, frames_until, quiet, serve
from tests.integration.stack import FAST_WORKER, Stack, eventually

pytestmark = pytest.mark.integration

DEAD_REDIS = "redis://127.0.0.1:1/0"


@contextlib.asynccontextmanager
async def serving(
    database_url: str,
    mosquitto_url: str,
    uow_factory: UnitOfWorkFactory,
    redis_url: str,
) -> AsyncIterator[tuple[Stack, str]]:
    """The platform behind a real socket, with a worker and a live view. Yields (stack, host)."""
    api_client = MqttClient(mosquitto_url, client_id=f"api-{uuid4().hex[:10]}")
    app = create_app(
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=database_url,
            drone_link="mqtt",
            mqtt_url=mosquitto_url,
            mqtt_ack_timeout_seconds=1.5,
            redis_url=redis_url,
            ws_ping_seconds=0.5,
        ),
        mqtt_client=api_client,
    )
    worker_redis = connect_redis(redis_url)
    live = RedisRealtime(worker_redis, state_ttl=timedelta(seconds=5))
    try:
        async with serve(app) as host:  # uvicorn runs the app's lifespan
            await api_client.wait_connected(5)
            async with httpx.AsyncClient(base_url=f"http://{host}") as http:
                stack = Stack(
                    http, app, mosquitto_url, uow_factory, live=live, worker_config=FAST_WORKER
                )
                await stack.start_worker()
                try:
                    yield stack, host
                finally:
                    await stack.close()
    finally:
        await worker_redis.aclose()


@pytest.fixture
async def served(
    database_url: str,
    mosquitto_url: str,
    pg_uow_factory: UnitOfWorkFactory,
    redis_url: str,
    redis_client: Any,  # flushes Redis before the test
) -> AsyncIterator[tuple[Stack, str]]:
    async with serving(database_url, mosquitto_url, pg_uow_factory, redis_url) as both:
        yield both


@pytest.fixture
async def served_without_redis(
    database_url: str, mosquitto_url: str, pg_uow_factory: UnitOfWorkFactory
) -> AsyncIterator[tuple[Stack, str]]:
    async with serving(database_url, mosquitto_url, pg_uow_factory, DEAD_REDIS) as both:
        yield both


async def dispatch(stack: Stack, client_id: str = "mock") -> UUID:
    order_id = await place_order(stack.http, client_id)
    body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()
    assert body["outcome"] == "started", body
    return order_id


class TestAnOrderIsFollowedAcrossTheWholeStack:
    async def test_the_platform_stream_shows_every_change_in_order_and_the_drone_moving(
        self, served: tuple[Stack, str]
    ) -> None:
        stack, host = served
        await stack.add_drone(speed_factor=300.0)  # a ~4 s sortie, 10 samples/s
        order_id = await place_order(stack.http)

        async with connect(host, f"/ws/internal/orders/{order_id}") as ws:
            snapshot = await frame(ws)
            await stack.http.post(f"/internal/orders/{order_id}/dispatch")
            frames = await frames_until(
                ws, lambda f: any(x.get("new_state") == "completed" for x in f), timeout=20
            )
            after = await quiet(ws, 0.6)

        assert snapshot["order"]["status"] == "created"
        audits = [f["new_state"] for f in frames if f["type"] == "audit"]
        assert audits == [
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
        positions = [f for f in frames if f["type"] == "position"]
        assert len(positions) >= 5
        assert {p["phase"] for p in positions} <= {"flying", "paused", "returning"}
        assert len({p["latitude"] for p in positions}) > 3  # it really moved
        assert all(p["altitude_m"] > 0 for p in positions)
        assert not [f for f in after if f["type"] == "position"]  # none after the flight ended

    async def test_a_partner_sees_its_own_vocabulary_and_a_position_only_while_carrying_it(
        self, served: tuple[Stack, str]
    ) -> None:
        stack, host = served
        drone = await stack.add_drone(speed_factor=300.0)
        order_id = await place_order(stack.http, "swiggy")

        async with connect(host, "/ws/partner/swiggy/orders/SW-100234") as ws:
            seen = [await frame(ws)]
            await stack.http.post(f"/internal/orders/{order_id}/dispatch")
            seen += await frames_until(
                ws, lambda f: bool(f) and f[-1]["data"]["order_status"] == "DELIVERED", timeout=20
            )
            code = await close_code(ws)

        assert code == 1000  # the stream ended with the order
        statuses = [f["data"]["order_status"] for f in seen]
        collapsed = [s for i, s in enumerate(statuses) if i == 0 or s != statuses[i - 1]]
        assert collapsed == [
            "PLACED",
            "DRONE_ASSIGNED",
            "PICKED_UP",
            "OUT_FOR_DELIVERY",
            "ARRIVING",
            "DELIVERED",
        ]
        located = [f for f in seen if f["data"]["live_location"] is not None]
        assert len(located) >= 3
        assert {f["data"]["order_status"] for f in located} <= {
            "PICKED_UP",
            "OUT_FOR_DELIVERY",
            "ARRIVING",
        }
        for f in seen:
            if f["data"]["order_status"] in {"PLACED", "DRONE_ASSIGNED", "DELIVERED"}:
                assert f["data"]["live_location"] is None
        text = json.dumps(seen)
        assert str(drone.drone_id) not in text
        for secret in ("battery", "flight_id", "permission", "mission", "phase"):
            assert secret not in text.lower(), secret

    async def test_the_fleet_stream_shows_samples_and_a_lost_link_then_its_recovery(
        self, served: tuple[Stack, str]
    ) -> None:
        stack, host = served
        drone = await stack.add_drone(speed_factor=1.0)
        await dispatch(stack)

        async with connect(host, "/ws/internal/fleet") as ws:
            snapshot = await frame(ws)
            assert [d["id"] for d in snapshot["drones"]] == [str(drone.drone_id)]

            await frames_until(
                ws,
                lambda f: any(
                    x["type"] == "drone_state" and x["drone_id"] == str(drone.drone_id) for x in f
                ),
            )
            stack.runners[drone.drone_id].muted.add(drone.drone_id)
            lost = await frames_until(
                ws, lambda f: any(x["type"] == "alert" and x["kind"] == "link_lost" for x in f)
            )
            stack.runners[drone.drone_id].muted.discard(drone.drone_id)
            recovered = await frames_until(
                ws,
                lambda f: any(
                    x["type"] == "alert" and x["kind"] == "link_lost" and x["resolved_at"]
                    for x in f
                ),
            )

        raised = next(f for f in lost if f["type"] == "alert" and f["kind"] == "link_lost")
        assert raised["drone_id"] == str(drone.drone_id)
        assert raised["resolved_at"] is None
        assert recovered  # and the recovery was announced too


class TestResilience:
    async def test_a_lost_redis_feed_closes_the_streams_which_then_resync(
        self, served: tuple[Stack, str], redis_client: Any
    ) -> None:
        stack, host = served
        await stack.add_drone(speed_factor=300.0)
        order_id = await place_order(stack.http)
        async with connect(host, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)

            await redis_client.client_kill_filter(_type="pubsub")  # the live feed drops
            code = await close_code(ws)

        assert code == 1013  # told to reconnect

        async def feed_is_back() -> bool:
            return bool(stack.app.state.hub.connected)

        await eventually(feed_is_back)
        async with connect(host, f"/ws/internal/orders/{order_id}") as ws:
            assert (await frame(ws))["type"] == "snapshot"  # a fresh snapshot
            await stack.http.post(f"/internal/orders/{order_id}/dispatch")
            frames = await frames_until(ws, lambda f: len(f) >= 3)
        assert [f["new_state"] for f in frames][:2] == ["planned", "permission_granted"]

    async def test_without_redis_new_streams_are_refused_politely_and_the_rest_works(
        self, served_without_redis: tuple[Stack, str]
    ) -> None:
        stack, host = served_without_redis
        await stack.add_drone()

        async with connect(host, "/ws/internal/fleet") as ws:
            error = await frame(ws)
            code = await close_code(ws)
        async with connect(host, "/ws/partner/mock/orders/ORD-1") as ws:
            partner_error = await frame(ws)
            partner_code = await close_code(ws)

        assert error["code"] == "live_view_unavailable"
        assert code == 1013
        assert partner_error["error"]["code"] == "unavailable"  # in the partner's own format
        assert partner_code == 1013
        order_id = await dispatch(stack)  # and delivery does not need the live view at all
        await eventually(lambda: _delivered(stack, order_id))


async def _delivered(stack: Stack, order_id: UUID) -> bool:
    return (await stack.order(order_id))["status"] == "delivered"
