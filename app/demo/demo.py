"""Wire the real platform to simulated drones, in one process.

Nothing here is new logic. It builds the real app (``create_app``) on the in-memory database,
connects its MQTT drone link to simulated drones through a loopback instead of a broker, and
feeds what the drones report (events and telemetry) to the same ingest and recorder the worker
uses. Because it is the real code, the rules hold here too: no flight without permission, no
steering, every change audited.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from app.compliance.mock_digital_sky import MockDigitalSky
from app.config import Settings
from app.dispatch.service import DispatchService
from app.domain.models import Location
from app.main import create_app
from app.protocol import messages as wire
from app.protocol import topics
from app.realtime.memory import InMemoryRealtime
from app.services.memory import InMemoryDatabase, InMemoryUnitOfWork
from app.simulator.drone import SimulatedDrone
from app.telemetry.ingest import TelemetryIngest
from app.telemetry.recorder import TelemetryRecorder

# A restaurant and a customer in Bangalore, and two drone docks.
PICKUP = Location(latitude=12.9716, longitude=77.5946)
DROPOFF = Location(latitude=12.9352, longitude=77.6245)
DOCKS = {
    "alpha": Location(latitude=12.9800, longitude=77.6000),
    "bravo": Location(latitude=12.9450, longitude=77.6100),
}

SAMPLE_EVERY_S = 0.25

Handler = Callable[[str, bytes], Awaitable[None]]


class LoopbackMqtt:
    """Stands in for an MQTT client *and* the broker: a command goes straight to the drone.

    The platform's real ``MqttDroneLink`` publishes to it, the simulated drone answers, and
    the answer is handed back to the link exactly as an acknowledgement from a broker would be.
    """

    def __init__(self) -> None:
        self.drones: dict[UUID, SimulatedDrone] = {}
        self._handlers: dict[str, Handler] = {}
        self.connected = False

    async def run(self, subscriptions: dict[str, Handler]) -> None:
        self._handlers = dict(subscriptions)
        self.connected = True
        try:
            await asyncio.Event().wait()
        finally:
            self.connected = False

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        drone = self.drones.get(topics.parse_command_topic(topic) or UUID(int=0))
        if drone is None:
            raise ConnectionError("no such drone on the loopback")
        command = wire.decode_command(payload)
        ack = drone.on_command(command)
        if ack is not None:
            await self._handlers[command.reply_to](command.reply_to, wire.encode(ack))


@dataclass
class Demo:
    app: FastAPI
    compliance: MockDigitalSky
    drones: dict[UUID, SimulatedDrone]
    names: dict[UUID, str]
    ingest: TelemetryIngest
    recorder: TelemetryRecorder
    live: InMemoryRealtime
    _since_sample: float = field(default=0.0)

    async def step(self, seconds: float) -> None:
        """Let the simulated world run for ``seconds``: drones fly, and report what happened."""
        for drone_id, drone in self.drones.items():
            for event in drone.advance(seconds):
                await self.ingest.handle_event(drone_id, wire.encode(event))
        self._since_sample += seconds
        if self._since_sample >= SAMPLE_EVERY_S:
            self._since_sample = 0.0
            for drone_id, drone in self.drones.items():
                await self.recorder.handle_sample(drone_id, wire.encode(drone.sample()))

    async def run_world(self, tick_s: float = 0.1) -> None:
        """Keep the world running until cancelled."""
        while True:
            await asyncio.sleep(tick_s)
            await self.step(tick_s)

    def set_airspace_closed(self, closed: bool) -> None:
        self.compliance.close_airspace("airspace closed for the demo" if closed else None)


async def build_demo(*, speed: float = 60.0) -> Demo:
    """Build the platform and its simulated drones. ``speed`` fast-forwards the drones' clock."""
    database = InMemoryDatabase()
    compliance = MockDigitalSky(validity=timedelta(hours=1))
    live = InMemoryRealtime()
    loopback = LoopbackMqtt()
    app = create_app(
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            drone_link="mqtt",
            mqtt_ack_timeout_seconds=2.0,
            ws_ping_seconds=10.0,
        ),
        uow_factory=lambda: InMemoryUnitOfWork(database),
        compliance=compliance,
        realtime=live,
        mqtt_client=loopback,  # type: ignore[arg-type]
    )
    dispatch: DispatchService = app.state.dispatch_service

    drones: dict[UUID, SimulatedDrone] = {}
    names: dict[UUID, str] = {}
    for name, home in DOCKS.items():
        registered = await dispatch.register_drone(
            name=name, home=home, max_payload_kg=5.0, max_range_km=20.0, battery_percent=100.0
        )
        drones[registered.id] = SimulatedDrone(
            registered.id, home=home, speed_factor=speed, hold_s=8.0, battery_drain_per_s=0.015
        )
        names[registered.id] = name
    loopback.drones = drones

    return Demo(
        app=app,
        compliance=compliance,
        drones=drones,
        names=names,
        ingest=TelemetryIngest(dispatch, alerts=live),
        recorder=TelemetryRecorder(dispatch, live, live),
        live=live,
    )


def demo_config(demo: Demo) -> dict[str, Any]:
    """What the page needs to draw the map."""
    return {
        "pickup": {"lat": PICKUP.latitude, "lng": PICKUP.longitude},
        "dropoff": {"lat": DROPOFF.latitude, "lng": DROPOFF.longitude},
        "docks": [
            {"id": str(i), "name": demo.names[i], "lat": d.home.latitude, "lng": d.home.longitude}
            for i, d in demo.drones.items()
        ],
    }
