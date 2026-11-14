"""Wire the real platform to simulated drones, in one process.

Nothing here is new logic. It builds the real app (``create_app``) on the in-memory database,
connects its MQTT drone link to simulated drones through a loopback instead of a broker, and
feeds what the drones report (events and telemetry) to the same ingest and recorder the worker
uses. Because it is the real code, the rules hold here too: no flight without permission, no
steering, every change audited.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from pydantic import SecretStr

from app.compliance.mock_digital_sky import MockDigitalSky
from app.config import Settings
from app.dispatch.service import DispatchService
from app.domain.models import AuditEventType, Location
from app.main import create_app
from app.protocol import messages as wire
from app.protocol import topics
from app.realtime.memory import InMemoryRealtime
from app.services.memory import InMemoryDatabase, InMemoryUnitOfWork
from app.services.webhook_service import WebhookService
from app.simulator.drone import SimulatedDrone
from app.telemetry.ingest import TelemetryIngest
from app.telemetry.recorder import TelemetryRecorder
from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.http import HttpxPoster
from app.webhooks.retry import RetryPolicy
from app.webhooks.sender import SenderConfig, WebhookSender
from app.webhooks.signing import verify

# A restaurant and a customer in Bangalore, and two drone docks.
PICKUP = Location(latitude=12.9716, longitude=77.5946)
DROPOFF = Location(latitude=12.9352, longitude=77.6245)
DOCKS = {
    "alpha": Location(latitude=12.9800, longitude=77.6000),
    "bravo": Location(latitude=12.9450, longitude=77.6100),
}

SAMPLE_EVERY_S = 0.25

# Webhooks: the demo plays a partner ("mock") with a server of its own, so the platform's real
# sender has someone to tell. Waits are short so a "down" partner is visible within seconds.
PARTNER_PATH = "/demo/partner/webhook"
DEMO_SECRET = "demo-secret"
DEMO_RETRY = RetryPolicy(max_attempts=12, base_s=1.0, factor=2.0, cap_s=4.0, jitter=0.1)
_ORDER_EVENTS = (AuditEventType.ORDER_CREATED, AuditEventType.ORDER_STATUS_CHANGED)

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
class MockPartner:
    """The partner's own server, as the demo plays it.

    It checks the signature, drops a repeat of an update it has already seen (what a partner
    should do, since delivery is at-least-once), and can be told to be down.
    """

    secret: str = DEMO_SECRET
    down: bool = False
    received: list[dict[str, Any]] = field(default_factory=list)
    refused: int = 0
    duplicates: int = 0
    _seen: set[str] = field(default_factory=set)

    def handle(self, *, body: bytes, headers: Mapping[str, str]) -> int:
        """Answer a webhook with an HTTP status code."""
        if self.down:
            self.refused += 1
            return 503
        try:
            payload = json.loads(body)
            signed = verify(
                self.secret,
                int(headers["x-timestamp"]),
                body,
                headers.get("x-signature", ""),
            )
        except (ValueError, KeyError):
            return 400
        event_id = headers.get("x-event-id", "")
        if event_id in self._seen:
            self.duplicates += 1
            return 200
        self._seen.add(event_id)
        self.received.append(
            {
                "event_id": event_id,
                "order_id": payload.get("order_id"),
                "status": payload.get("status"),
                "attempt": int(headers.get("x-delivery-attempt", "1")),
                "signature_ok": signed,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )
        return 204


@dataclass
class Demo:
    app: FastAPI
    compliance: MockDigitalSky
    drones: dict[UUID, SimulatedDrone]
    names: dict[UUID, str]
    ingest: TelemetryIngest
    recorder: TelemetryRecorder
    live: InMemoryRealtime
    database: InMemoryDatabase
    partner: MockPartner
    sender: WebhookSender
    poster: HttpxPoster
    webhooks: WebhookService
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

    async def run_webhooks(self) -> None:
        """Keep telling the partner about order changes until cancelled."""
        await self.sender.run()

    def set_airspace_closed(self, closed: bool) -> None:
        self.compliance.close_airspace("airspace closed for the demo" if closed else None)

    async def partner_view(self) -> dict[str, Any]:
        """The webhooks as the page shows them: each update, and how its delivery is going.

        The deliveries are the platform's real ones (the same service the internal API uses);
        the status of each update is read from the audit trail held in this in-memory database.
        """
        deliveries = await self.webhooks.list_deliveries(client_id="mock", limit=14)
        statuses = {
            e.event_id: e.new_state.value
            for e in self.database.audit
            if e.event_type in _ORDER_EVENTS and e.new_state is not None
        }
        now = datetime.now(UTC)
        updates = [
            {
                "event_id": str(d.event_id),
                "update": statuses.get(d.event_id, "?").replace("_", " "),
                "delivery": d.status.value,
                "attempts": d.attempts,
                "error": d.last_error,
                "retry_in_s": (
                    max(0.0, round((d.next_attempt_at - now).total_seconds(), 1))
                    if d.status.value == "pending" and d.attempts > 0
                    else None
                ),
            }
            for d in deliveries
        ]
        return {
            "down": self.partner.down,
            "updates": updates,
            "received": len(self.partner.received),
            "refused": self.partner.refused,
            "duplicates": self.partner.duplicates,
        }


async def build_demo(
    *,
    speed: float = 60.0,
    webhook_url: str | None = None,
    webhook_policy: RetryPolicy | None = None,
) -> Demo:
    """Build the platform and its simulated drones. ``speed`` fast-forwards the drones' clock.

    The platform's webhook sender tells the demo's own mock partner about each order change.
    With no ``webhook_url`` it posts to the app in-process (no socket: for tests); the server
    passes the real URL, so the demo makes a real HTTP request to itself.
    """
    database = InMemoryDatabase()
    compliance = MockDigitalSky(validity=timedelta(hours=1))
    live = InMemoryRealtime()
    loopback = LoopbackMqtt()

    def uow_factory() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(database)

    app = create_app(
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            drone_link="mqtt",
            mqtt_ack_timeout_seconds=2.0,
            ws_ping_seconds=10.0,
        ),
        uow_factory=uow_factory,
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

    if webhook_url is None:
        poster = HttpxPoster.in_process(app, timeout_s=5.0)
        webhook_url = f"http://demo.invalid{PARTNER_PATH}"
    else:
        poster = HttpxPoster(timeout_s=5.0, connect_timeout_s=2.0)
    sender = WebhookSender(
        uow_factory,
        registry=app.state.adapter_registry,
        endpoints={"mock": WebhookEndpoint(url=webhook_url, secret=SecretStr(DEMO_SECRET))},
        poster=poster,
        policy=webhook_policy or DEMO_RETRY,
        config=SenderConfig(batch_size=10, lease=timedelta(seconds=30), concurrency=4, poll_s=0.2),
    )

    return Demo(
        app=app,
        compliance=compliance,
        drones=drones,
        names=names,
        ingest=TelemetryIngest(dispatch, alerts=live),
        recorder=TelemetryRecorder(dispatch, live, live),
        live=live,
        database=database,
        partner=MockPartner(),
        sender=sender,
        poster=poster,
        webhooks=app.state.webhook_service,
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
