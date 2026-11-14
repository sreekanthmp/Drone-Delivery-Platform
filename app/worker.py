"""The telemetry worker: a separate process that records what drones report.

Run with::

    python -m app.worker

It subscribes (with a durable session, so events survive a short restart) to every drone's
event topic and gives each message to the telemetry ingest. It is a composition root like
``app.main``, so it may import across layers, but it is deliberately powerless: its
dispatch service gets a link and a compliance provider that refuse everything, so this
process can never send a command to a drone or ask for permission (Rule 1).

Run exactly **one** worker per broker: MQTT 3.1.1 has no shared subscriptions, and two
clients with the same identity would take the session from each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from uuid import UUID

import structlog

from app.compliance.disabled import DisabledCompliance
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.db.uow import SqlAlchemyUnitOfWork
from app.dispatch.link import DisabledDroneLink
from app.dispatch.service import DispatchService
from app.logging import configure_logging
from app.mqtt.client import MqttClient
from app.protocol import topics
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from app.telemetry.ingest import TelemetryIngest

logger = structlog.get_logger(__name__)


class Worker:
    def __init__(self, uow_factory: UnitOfWorkFactory, client: MqttClient) -> None:
        self._client = client
        dispatch = DispatchService(
            uow_factory,
            orders=OrderService(uow_factory),
            compliance=DisabledCompliance(),
            link=DisabledDroneLink(),
        )
        self.ingest = TelemetryIngest(dispatch)

    async def on_event(self, topic: str, payload: bytes) -> None:
        drone_id: UUID | None = topics.parse_event_topic(topic)
        if drone_id is None:
            logger.warning("event_topic_invalid", topic=topic)
            return
        await self.ingest.handle_event(drone_id, payload)

    async def run(self) -> None:
        """Serve until cancelled."""
        await self._client.run({topics.EVENTS_FILTER: self.on_event})


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    client = MqttClient(settings.mqtt_url, client_id=settings.mqtt_worker_client_id, durable=True)
    worker = Worker(lambda: SqlAlchemyUnitOfWork(session_factory), client)
    task = asyncio.create_task(worker.run())
    logger.info("worker_started", mqtt_url=settings.mqtt_url)
    try:
        await stop.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await engine.dispose()
        logger.info("worker_stopped", counts={k.value: v for k, v in worker.ingest.counts.items()})


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    async def serve() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(settings, stop)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
