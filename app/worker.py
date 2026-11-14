"""The telemetry worker: a separate process that records what drones report.

Run with::

    python -m app.worker

It subscribes (with a durable session, so events survive a short restart) to every drone's
event topic and gives each message to the telemetry ingest. With Redis configured it also
records every drone's telemetry samples (live state, battery sync), runs the watchdog that
alerts on lost links, low batteries and disagreements, and shares the live view with the API.
A reaper job releases reservations that a crashed dispatch left behind.

It is a composition root like ``app.main``, so it may import across layers, but it is
deliberately powerless: its dispatch service gets a link and a compliance provider that
refuse everything, so this process can never send a command to a drone or ask for permission
(Rule 1). The watchdog and reaper only alert, or cancel flights that never took off.

Run exactly **one** worker per broker: MQTT 3.1.1 has no shared subscriptions, and two
clients with the same identity would take the session from each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import timedelta
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
from app.realtime.connection import connect
from app.realtime.memory import InMemoryRealtime
from app.realtime.notifier import AuditBroadcaster
from app.realtime.ports import LiveView
from app.realtime.redis_backend import RedisRealtime
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from app.telemetry.ingest import TelemetryIngest
from app.telemetry.periodic import every
from app.telemetry.reaper import Reaper
from app.telemetry.recorder import TelemetryRecorder
from app.telemetry.watchdog import Watchdog, WatchdogConfig

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    battery_step_percent: float = 5.0
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    watchdog_interval_s: float = 5.0
    reaper_interval_s: float = 60.0
    stale_after: timedelta = timedelta(minutes=5)

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerConfig:
        return cls(
            battery_step_percent=settings.battery_sync_step_percent,
            watchdog=WatchdogConfig(
                link_lost_after=timedelta(seconds=settings.link_lost_after_seconds),
                low_battery_percent=settings.low_battery_percent,
                mismatch_grace=timedelta(seconds=settings.state_mismatch_grace_seconds),
            ),
            watchdog_interval_s=settings.watchdog_interval_seconds,
            reaper_interval_s=settings.reaper_interval_seconds,
            stale_after=timedelta(minutes=settings.stale_reservation_minutes),
        )


class Worker:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        client: MqttClient,
        *,
        live: LiveView | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._client = client
        self._config = config or WorkerConfig()
        notifier = AuditBroadcaster(live) if live is not None else None
        dispatch = DispatchService(
            uow_factory,
            orders=OrderService(uow_factory, notifier=notifier),
            compliance=DisabledCompliance(),
            link=DisabledDroneLink(),
            notifier=notifier,
        )
        self.ingest = TelemetryIngest(dispatch, alerts=live)
        self.recorder: TelemetryRecorder | None = None
        self.watchdog: Watchdog | None = None
        if live is not None:
            self.recorder = TelemetryRecorder(
                dispatch, live, live, battery_step_percent=self._config.battery_step_percent
            )
            self.watchdog = Watchdog(dispatch, live, live, config=self._config.watchdog)
        # Without Redis the reaper still runs; its alerts go to the log only.
        self.reaper = Reaper(
            dispatch,
            live if live is not None else InMemoryRealtime(),
            older_than=self._config.stale_after,
        )

    async def on_event(self, topic: str, payload: bytes) -> None:
        drone_id: UUID | None = topics.parse_event_topic(topic)
        if drone_id is None:
            logger.warning("event_topic_invalid", topic=topic)
            return
        await self.ingest.handle_event(drone_id, payload)

    async def on_sample(self, topic: str, payload: bytes) -> None:
        drone_id: UUID | None = topics.parse_telemetry_topic(topic)
        if drone_id is None or self.recorder is None:
            logger.warning("telemetry_topic_invalid", topic=topic)
            return
        await self.recorder.handle_sample(drone_id, payload)

    async def run(self) -> None:
        """Serve until cancelled: the MQTT subscriptions, the watchdog and the reaper."""
        subscriptions = {topics.EVENTS_FILTER: self.on_event}
        jobs = [
            asyncio.create_task(
                every(self._config.reaper_interval_s, self.reaper.sweep, name="reaper")
            )
        ]
        if self.recorder is not None and self.watchdog is not None:
            subscriptions[topics.TELEMETRY_FILTER] = self.on_sample
            jobs.append(
                asyncio.create_task(
                    every(self._config.watchdog_interval_s, self.watchdog.check, name="watchdog")
                )
            )
        try:
            await self._client.run(subscriptions)
        finally:
            for job in jobs:
                job.cancel()
            for job in jobs:
                with contextlib.suppress(asyncio.CancelledError):
                    await job


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine_from_settings(settings)
    session_factory = create_session_factory(engine)
    redis_client = connect(settings.redis_url) if settings.redis_url else None
    live = (
        RedisRealtime(redis_client, state_ttl=timedelta(seconds=settings.live_state_ttl_seconds))
        if redis_client is not None
        else None
    )
    client = MqttClient(settings.mqtt_url, client_id=settings.mqtt_worker_client_id, durable=True)
    worker = Worker(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        client,
        live=live,
        config=WorkerConfig.from_settings(settings),
    )
    task = asyncio.create_task(worker.run())
    logger.info("worker_started", mqtt_url=settings.mqtt_url, live_view=live is not None)
    try:
        await stop.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if redis_client is not None:
            await redis_client.aclose()
        await engine.dispose()
        logger.info(
            "worker_stopped",
            events={k.value: v for k, v in worker.ingest.counts.items()},
            samples=(
                {k.value: v for k, v in worker.recorder.counts.items()}
                if worker.recorder is not None
                else None
            ),
        )


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
