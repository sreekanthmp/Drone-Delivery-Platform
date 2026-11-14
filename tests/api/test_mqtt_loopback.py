"""``DRONE_LINK=mqtt`` wiring, and the whole chain without a broker.

``LoopbackMqtt`` stands in for the MQTT client *and* the broker: a command the real
``MqttDroneLink`` publishes is handed to a ``SimulatedDrone``, whose acknowledgement is
delivered back to the link; the drone's events are fed to the real ``TelemetryIngest``. So
HTTP -> dispatch -> link -> drone -> events -> ingest -> order is exercised end to end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.mqtt_link import MqttDroneLink
from app.main import create_app
from app.protocol import messages as wire
from app.protocol import topics
from app.simulator.drone import Phase, SimulatedDrone
from app.telemetry.ingest import IngestOutcome, TelemetryIngest
from tests.api.conftest import place_order, register_drone
from tests.conftest import HOME
from tests.dispatch_rig import ScriptedCompliance
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork


class LoopbackMqtt:
    def __init__(self) -> None:
        self.drones: dict[UUID, SimulatedDrone] = {}
        self.handlers: dict[str, Any] = {}
        self.connected = False
        self.down = False
        self.sent: list[str] = []

    async def run(self, subscriptions: dict[str, Any]) -> None:
        self.handlers = dict(subscriptions)
        self.connected = True
        try:
            await asyncio.Event().wait()
        finally:
            self.connected = False

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        if self.down:
            raise ConnectionError("not connected to the MQTT broker")
        command = wire.decode_command(payload)
        self.sent.append(command.type)
        drone = self.drones[topics.parse_command_topic(topic) or UUID(int=0)]
        ack = drone.on_command(command)
        if ack is not None:
            await self.handlers[command.reply_to](command.reply_to, wire.encode(ack))


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, drone_link="mqtt", mqtt_ack_timeout_seconds=0.2, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def loopback() -> LoopbackMqtt:
    return LoopbackMqtt()


@pytest.fixture
async def mqtt_app(loopback: LoopbackMqtt) -> AsyncIterator[FastAPI]:
    database = InMemoryDatabase()
    app = create_app(
        settings=settings(),
        uow_factory=lambda: InMemoryUnitOfWork(database),
        compliance=ScriptedCompliance(validity=timedelta(hours=1)),
        mqtt_client=loopback,  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if loopback.connected:
                break
            await asyncio.sleep(0.005)
        yield app


@pytest.fixture
async def http(mqtt_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mqtt_app), base_url="http://platform.test"
    ) as client:
        yield client


async def sim_drone(
    http: httpx.AsyncClient, loopback: LoopbackMqtt, **overrides: Any
) -> SimulatedDrone:
    registered = await register_drone(http)
    drone = SimulatedDrone(UUID(registered["id"]), home=HOME, speed_factor=10_000, **overrides)
    loopback.drones[drone.drone_id] = drone
    return drone


async def feed(app: FastAPI, drone: SimulatedDrone, seconds: float) -> list[IngestOutcome]:
    """Let the drone fly for a while and hand every event it reports to the real ingest."""
    ingest = TelemetryIngest(app.state.dispatch_service)
    results = []
    for event in drone.advance(seconds):
        results.append((await ingest.handle_event(drone.drone_id, wire.encode(event))).outcome)
    return results


class TestWiring:
    def test_the_in_memory_link_is_the_default(self) -> None:
        app = create_app(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            uow_factory=lambda: InMemoryUnitOfWork(InMemoryDatabase()),
        )

        assert isinstance(app.state.drone_link, InMemoryDroneLink)

    async def test_the_mqtt_link_is_used_when_configured(self, mqtt_app: FastAPI) -> None:
        assert isinstance(mqtt_app.state.drone_link, MqttDroneLink)

    def test_an_explicit_link_wins_over_the_setting(self) -> None:
        link = InMemoryDroneLink()

        app = create_app(
            settings=settings(),
            uow_factory=lambda: InMemoryUnitOfWork(InMemoryDatabase()),
            drone_link=link,
        )

        assert app.state.drone_link is link

    async def test_the_app_listens_for_acknowledgements_on_the_links_reply_topic(
        self, mqtt_app: FastAPI, loopback: LoopbackMqtt
    ) -> None:
        link = mqtt_app.state.drone_link

        assert set(loopback.handlers) == {link.reply_topic}
        assert topics.is_ack_topic(link.reply_topic)

    async def test_readiness_follows_the_broker_connection(
        self, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        assert (await http.get("/ready")).status_code == 200

        loopback.connected = False
        assert (await http.get("/ready")).status_code == 503

    async def test_the_client_task_is_stopped_with_the_app(self, loopback: LoopbackMqtt) -> None:
        database = InMemoryDatabase()
        app = create_app(
            settings=settings(),
            uow_factory=lambda: InMemoryUnitOfWork(database),
            mqtt_client=loopback,  # type: ignore[arg-type]
        )

        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.02)
            assert loopback.connected

        assert not loopback.connected


class TestTheWholeChain:
    async def test_dispatch_flight_and_delivery_through_the_real_link_and_ingest(
        self, mqtt_app: FastAPI, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        drone = await sim_drone(http, loopback)
        order_id = await place_order(http)

        dispatched = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert dispatched["outcome"] == "started"
        assert loopback.sent == ["upload_mission", "start_mission"]
        assert drone.phase is Phase.FLYING

        outcomes = await feed(mqtt_app, drone, 100_000)

        assert outcomes == [IngestOutcome.APPLIED] * 5
        order = (await http.get(f"/internal/orders/{order_id}")).json()
        flights = (await http.get(f"/internal/orders/{order_id}/flights")).json()
        fleet = (await http.get("/internal/drones")).json()
        assert order["status"] == "delivered"
        assert [f["status"] for f in flights] == ["completed"]
        assert [d["status"] for d in fleet] == ["available"]  # no manual 'docked' needed

    async def test_a_cancelled_order_recalls_the_drone_which_then_docks_itself(
        self, mqtt_app: FastAPI, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        drone = await sim_drone(http, loopback)
        order_id = await place_order(http)
        await http.post(f"/internal/orders/{order_id}/dispatch")

        cancelled = await http.post("/partner/mock/orders/ORD-1/cancel")

        assert cancelled.status_code == 200
        assert loopback.sent[-1] == "return_to_home"
        assert drone.phase is Phase.RETURNING
        fleet = (await http.get("/internal/drones")).json()
        assert [d["status"] for d in fleet] == ["returning"]

        assert await feed(mqtt_app, drone, 100_000) == [IngestOutcome.APPLIED]  # 'docked'
        fleet = (await http.get("/internal/drones")).json()
        assert [d["status"] for d in fleet] == ["available"]

    async def test_a_drone_that_refuses_the_mission_fails_the_upload(
        self, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        drone = await sim_drone(http, loopback)
        drone.refuse["upload_mission"] = "storage full"
        order_id = await place_order(http)

        body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "upload_failed"
        assert "storage full" in body["detail"]
        assert body["drone"]["status"] == "available"
        assert body["order"]["status"] == "created"

    async def test_a_drone_that_never_answers_a_start_is_recalled(
        self, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        drone = await sim_drone(http, loopback)
        drone.silent.add("start_mission")  # it starts, but the acknowledgement is lost
        order_id = await place_order(http)

        body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "start_failed"
        assert body["order"]["status"] == "failed"
        assert body["flight"]["status"] == "aborted"
        assert body["drone"]["status"] == "returning"
        assert loopback.sent == ["upload_mission", "start_mission", "return_to_home"]
        assert drone.phase is Phase.RETURNING  # it really had started, and was brought back

    async def test_an_unreachable_broker_means_nothing_was_sent(
        self, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        await sim_drone(http, loopback)
        loopback.down = True
        order_id = await place_order(http)

        body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "upload_failed"
        assert body["drone"]["status"] == "available"
        assert loopback.sent == []

    async def test_pause_and_resume_reach_the_drone(
        self, http: httpx.AsyncClient, loopback: LoopbackMqtt
    ) -> None:
        drone = await sim_drone(http, loopback)
        order_id = await place_order(http)
        flight_id = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()["flight"][
            "id"
        ]

        paused = await http.post(f"/internal/flights/{flight_id}/pause")
        assert paused.json()["status"] == "paused"
        assert drone.phase is Phase.PAUSED

        resumed = await http.post(f"/internal/flights/{flight_id}/resume")
        assert resumed.json()["status"] == "started"
        assert drone.phase is Phase.FLYING
