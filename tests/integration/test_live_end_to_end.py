"""Live tracking on real infrastructure: PostgreSQL, Mosquitto and Redis, all together.

Simulated drones fly through a real broker; the worker records their samples into a real
Redis; the API serves alerts from it. These tests prove that the live view reflects reality,
that the watchdog only ever *alerts*, that the reaper frees abandoned reservations, and that
losing Redis costs live tracking and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.mqtt.client import MqttClient
from app.protocol import messages as wire
from app.protocol import topics
from app.realtime import channels
from app.realtime.connection import connect
from app.realtime.models import DroneState, Envelope
from app.realtime.redis_backend import RedisRealtime
from app.services.ports import UnitOfWorkFactory
from app.simulator.drone import Phase
from app.telemetry.watchdog import WatchdogConfig
from app.worker import WorkerConfig
from tests.api.conftest import place_order
from tests.conftest import T0
from tests.integration.conftest import RunSql
from tests.integration.flights import FlightFlow
from tests.integration.stack import Stack, audit, eventually, is_, order_is

pytestmark = pytest.mark.integration

FAST_WORKER = WorkerConfig(
    battery_step_percent=5.0,
    watchdog=WatchdogConfig(
        link_lost_after=timedelta(seconds=1.0),
        low_battery_percent=20.0,
        mismatch_grace=timedelta(seconds=1.5),
    ),
    watchdog_interval_s=0.2,
    reaper_interval_s=0.2,
    stale_after=timedelta(minutes=5),
)


@contextlib.asynccontextmanager
async def running(
    database_url: str,
    mosquitto_url: str,
    uow_factory: UnitOfWorkFactory,
    redis_url: str,
) -> AsyncIterator[Stack]:
    api_client = MqttClient(mosquitto_url, client_id=f"api-{uuid4().hex[:10]}")
    app = create_app(
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=database_url,
            drone_link="mqtt",
            mqtt_url=mosquitto_url,
            mqtt_ack_timeout_seconds=1.5,
            redis_url=redis_url,
        ),
        mqtt_client=api_client,
    )
    worker_redis = connect(redis_url)
    live = RedisRealtime(worker_redis, state_ttl=timedelta(seconds=5))
    try:
        async with app.router.lifespan_context(app):
            await api_client.wait_connected(5)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://platform.test"
            ) as http:
                built = Stack(
                    http, app, mosquitto_url, uow_factory, live=live, worker_config=FAST_WORKER
                )
                await built.start_worker()
                try:
                    yield built
                finally:
                    await built.close()
    finally:
        await worker_redis.aclose()


@pytest.fixture
async def stack(
    database_url: str,
    mosquitto_url: str,
    pg_uow_factory: UnitOfWorkFactory,
    redis_url: str,
    redis_client: Any,  # flushes Redis before the test
) -> AsyncIterator[Stack]:
    async with running(database_url, mosquitto_url, pg_uow_factory, redis_url) as built:
        yield built


@pytest.fixture
async def stack_without_redis(
    database_url: str, mosquitto_url: str, pg_uow_factory: UnitOfWorkFactory
) -> AsyncIterator[Stack]:
    """Everything configured for Redis, but nothing is listening on its port."""
    async with running(database_url, mosquitto_url, pg_uow_factory, "redis://127.0.0.1:1/0") as b:
        yield b


class Watcher:
    """Collects what is published on Redis channels while a test runs."""

    def __init__(self, redis_client: Any) -> None:
        self._pubsub = redis_client.pubsub()
        self.messages: list[tuple[str, Envelope]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self, *channel_names: str) -> Watcher:
        await self._pubsub.subscribe(*channel_names)
        self._task = asyncio.create_task(self._read())
        await asyncio.sleep(0.1)  # let the subscription settle
        return self

    async def _read(self) -> None:
        async for message in self._pubsub.listen():
            if message["type"] == "message":
                self.messages.append((message["channel"], Envelope.decode(message["data"])))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._pubsub.aclose()

    def of(self, kind: str, channel: str | None = None) -> list[Envelope]:
        return [e for c, e in self.messages if e.kind == kind and channel in (None, c)]


async def dispatch(stack: Stack) -> tuple[UUID, str]:
    order_id = await place_order(stack.http)
    body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()
    assert body["outcome"] == "started", body
    return order_id, body["flight"]["id"]


async def active_kinds(stack: Stack) -> set[str]:
    return {a["kind"] for a in (await stack.alerts())["active"]}


async def state_of(redis_client: Any, drone_id: UUID) -> DroneState | None:
    raw = await redis_client.get(channels.state_key(drone_id))
    return None if raw is None else DroneState.model_validate_json(raw)


class TestSamplesReachTheLiveView:
    async def test_the_latest_state_follows_the_drone_and_is_streamed_to_the_fleet(
        self, stack: Stack, redis_client: Any
    ) -> None:
        drone = await stack.add_drone(speed_factor=300.0)
        watcher = await Watcher(redis_client).start(channels.FLEET)
        try:
            await dispatch(stack)

            async def airborne() -> bool:
                state = await state_of(redis_client, drone.drone_id)
                return state is not None and state.phase is wire.DronePhase.FLYING

            await eventually(airborne)
            state = await state_of(redis_client, drone.drone_id)
            assert state is not None
            assert state.flight_id is not None
            assert state.altitude_m > 0
            assert (state.latitude, state.longitude) != (12.98, 77.60)  # it has left the dock

            async def home_again() -> bool:
                latest = await state_of(redis_client, drone.drone_id)
                return latest is not None and latest.phase is wire.DronePhase.IDLE

            await eventually(home_again)
        finally:
            await watcher.stop()

        samples = [
            e for e in watcher.of("drone_state") if e.data["drone_id"] == str(drone.drone_id)
        ]
        assert len(samples) >= 5
        assert {e.droppable for e in samples} == {True}
        assert {e.data["phase"] for e in samples} >= {"flying", "idle"}

    async def test_the_stored_battery_follows_the_drone_in_coarse_steps(self, stack: Stack) -> None:
        drone = await stack.add_drone(speed_factor=300.0, battery_drain_per_s=0.05)
        assert (await stack.drone(drone.drone_id))["battery_percent"] == 90.0  # as registered
        await dispatch(stack)

        async def drained() -> bool:
            return float((await stack.drone(drone.drone_id))["battery_percent"]) < 85.0

        await eventually(drained)

        async def recharged() -> bool:  # back on the dock with a fresh battery
            return float((await stack.drone(drone.drone_id))["battery_percent"]) == 100.0

        await eventually(recharged)

    async def test_every_committed_change_is_announced_in_order_across_both_processes(
        self, stack: Stack, redis_client: Any
    ) -> None:
        await stack.add_drone()
        order_id = await place_order(stack.http)
        watcher = await Watcher(redis_client).start(channels.order_channel(order_id))
        try:
            await stack.http.post(f"/internal/orders/{order_id}/dispatch")
            await eventually(lambda: order_is(stack, order_id, "delivered"))

            async def completed() -> bool:
                return len(watcher.of("audit")) >= 10

            await eventually(completed)
        finally:
            await watcher.stop()

        # dispatch happens in the API process, progress in the worker: one ordered story
        assert [e.data["new_state"] for e in watcher.of("audit")] == [
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

    async def test_samples_from_a_stranger_are_ignored(
        self, stack: Stack, redis_client: Any
    ) -> None:
        stranger = uuid4()
        sample = wire.Telemetry(
            sent_at=T0,
            latitude=1.0,
            longitude=2.0,
            altitude_m=3.0,
            battery_percent=50.0,
            phase=wire.DronePhase.FLYING,
        )

        await stack.publish_raw(topics.telemetry_topic(stranger), wire.encode(sample), qos=1)

        async def refused() -> bool:
            assert stack.worker is not None
            assert stack.worker.recorder is not None
            return stack.worker.recorder.counts["unknown_drone"] >= 1

        await eventually(refused)
        assert await state_of(redis_client, stranger) is None
        assert not [k async for k in redis_client.scan_iter(match=f"*{stranger}*")]


class TestTheWatchdogOnlyAlerts:
    async def test_a_drone_that_falls_silent_in_the_air_raises_link_lost_and_clears_on_return(
        self, stack: Stack
    ) -> None:
        drone = await stack.add_drone(speed_factor=1.0)  # slow: it stays in the air
        _, flight_id = await dispatch(stack)
        await eventually(lambda: _heard(stack, drone.drone_id))

        stack.runners[drone.drone_id].muted.add(drone.drone_id)  # the link goes quiet

        await eventually(lambda: _has(stack, "link_lost"))
        # alerts only: the drone is still flying and the platform still says so
        assert drone.phase is Phase.FLYING
        flight = (await stack.http.get(f"/internal/flights/{flight_id}")).json()
        assert flight["status"] == "started"

        stack.runners[drone.drone_id].muted.discard(drone.drone_id)  # back on the air
        await eventually(lambda: _lacks(stack, "link_lost"))
        recent = (await stack.alerts())["recent"]
        assert any(a["kind"] == "link_lost" and a["resolved_at"] is not None for a in recent)

    async def test_a_low_battery_in_the_air_is_an_alert_and_nothing_more(
        self, stack: Stack
    ) -> None:
        drone = await stack.add_drone(speed_factor=1.0, battery_percent=15.0)
        _, flight_id = await dispatch(stack)  # selection saw the registered 90%

        await eventually(lambda: _has(stack, "low_battery"))

        assert drone.phase is Phase.FLYING
        assert (await stack.http.get(f"/internal/flights/{flight_id}")).json()[
            "status"
        ] == "started"

    async def test_a_drone_that_says_it_has_landed_while_the_platform_says_flying_is_flagged(
        self, stack: Stack
    ) -> None:
        drone = await stack.add_drone(speed_factor=1.0)
        _, flight_id = await dispatch(stack)
        await eventually(lambda: _heard(stack, drone.drone_id))

        drone.phase = Phase.LANDED  # a failsafe landing nobody told the platform about

        await eventually(lambda: _has(stack, "state_mismatch"))
        alert = next(a for a in (await stack.alerts())["active"] if a["kind"] == "state_mismatch")
        assert alert["flight_id"] == flight_id
        assert (await stack.http.get(f"/internal/flights/{flight_id}")).json()[
            "status"
        ] == "started"

    async def test_a_reported_fault_is_an_alert_and_changes_nothing(self, stack: Stack) -> None:
        drone = await stack.add_drone(speed_factor=1.0)
        _, flight_id = await dispatch(stack)
        event = wire.DroneEvent(
            event_id=uuid4(),
            flight_id=UUID(flight_id),
            seq=1,
            type=wire.EventType.FAULT,
            occurred_at=T0,
            detail={"code": "GPS_LOSS", "message": "no fix for 20 s"},
        )

        await stack.publish_raw(topics.event_topic(drone.drone_id), wire.encode(event))

        async def raised() -> bool:
            return any(a["kind"] == "fault" for a in (await stack.alerts())["recent"])

        await eventually(raised)
        fault = next(a for a in (await stack.alerts())["recent"] if a["kind"] == "fault")
        assert fault["message"] == "GPS_LOSS: no fix for 20 s"
        assert fault["flight_id"] == flight_id
        assert (await stack.http.get(f"/internal/flights/{flight_id}")).json()[
            "status"
        ] == "started"
        assert drone.phase is Phase.FLYING


class TestTheReaper:
    async def test_it_frees_a_reservation_a_crashed_dispatch_left_behind(
        self, stack: Stack, run_sql: RunSql
    ) -> None:
        drone = await stack.add_drone(play=False)
        order_id = await place_order(stack.http)
        ten_minutes_ago = datetime.now(UTC) - timedelta(minutes=10)
        abandoned = await FlightFlow(stack.uow_factory, clock=lambda: ten_minutes_ago).plan(
            order_id, drone.drone_id
        )

        async def released() -> bool:
            flights = await stack.flights(order_id)
            return [f["status"] for f in flights] == ["cancelled"]

        await eventually(released)

        trail = await audit(run_sql, order_id)
        assert trail[-1] == ("flight_status_changed", "cancelled", "system")
        recent = (await stack.alerts())["recent"]
        stale = [a for a in recent if a["kind"] == "stale_reservation"]
        assert [a["flight_id"] for a in stale] == [str(abandoned.id)]
        assert (await stack.order(order_id))["status"] == "created"

    async def test_the_order_can_then_be_dispatched_normally(self, stack: Stack) -> None:
        drone = await stack.add_drone()
        order_id = await place_order(stack.http)
        long_ago = datetime.now(UTC) - timedelta(minutes=10)
        await FlightFlow(stack.uow_factory, clock=lambda: long_ago).plan(order_id, drone.drone_id)
        await eventually(lambda: _flight_count(stack, order_id, "cancelled", 1))

        body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "started"
        await eventually(lambda: order_is(stack, order_id, "delivered"))

    async def test_a_flight_in_the_air_is_never_reaped(self, stack: Stack) -> None:
        await stack.add_drone(speed_factor=1.0)
        order_id, flight_id = await dispatch(stack)

        await asyncio.sleep(1.0)  # several reaper sweeps

        assert (await stack.http.get(f"/internal/flights/{flight_id}")).json()[
            "status"
        ] == "started"
        assert (await stack.order(order_id))["status"] == "assigned"


class TestRedisBeingDownCostsOnlyLiveTracking:
    async def test_dispatch_flight_delivery_and_the_audit_trail_all_still_work(
        self, stack_without_redis: Stack, run_sql: RunSql
    ) -> None:
        stack = stack_without_redis
        drone = await stack.add_drone()

        order_id, _ = await dispatch(stack)
        await eventually(lambda: order_is(stack, order_id, "delivered"))
        await eventually(lambda: is_(stack.drone_status(drone.drone_id), "available"))

        trail = await audit(run_sql, order_id)
        assert [state for _, state, _ in trail][-5:] == [
            "picked_up",
            "in_flight",
            "arriving",
            "delivered",
            "completed",
        ]

    async def test_readiness_does_not_depend_on_it_but_the_alerts_say_it_is_down(
        self, stack_without_redis: Stack
    ) -> None:
        stack = stack_without_redis

        assert (await stack.http.get("/ready")).status_code == 200
        response = await stack.http.get("/internal/alerts")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "live_view_unavailable"

    async def test_the_worker_and_its_jobs_keep_running(self, stack_without_redis: Stack) -> None:
        stack = stack_without_redis
        await stack.add_drone()
        await asyncio.sleep(1.0)  # several failing watchdog passes and sample writes

        order_id, _ = await dispatch(stack)

        await eventually(lambda: order_is(stack, order_id, "delivered"))


# --- predicates ---------------------------------------------------------------------------------


async def _heard(stack: Stack, drone_id: UUID) -> bool:
    assert stack.live is not None
    return await stack.live.get_state(drone_id) is not None


async def _has(stack: Stack, kind: str) -> bool:
    return kind in await active_kinds(stack)


async def _lacks(stack: Stack, kind: str) -> bool:
    return kind not in await active_kinds(stack)


async def _flight_count(stack: Stack, order_id: UUID, status: str, count: int) -> bool:
    flights = await stack.flights(order_id)
    return [f["status"] for f in flights].count(status) >= count
