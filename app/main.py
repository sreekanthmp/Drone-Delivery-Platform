"""Application factory and composition root.

Run with::

    uvicorn app.main:create_app --factory

This is the one place that wires the layers together (settings, database, adapters,
services, routers), so it is the only module allowed to import across all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import structlog
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.registry import AdapterRegistry, default_registry
from app.api import health
from app.api.errors import register_error_handlers
from app.api.internal import alerts as internal_alerts
from app.api.internal import drones as internal_drones
from app.api.internal import flights as internal_flights
from app.api.internal import orders as internal_orders
from app.api.partner import router as partner
from app.api.ws import internal as ws_internal
from app.api.ws import partner as ws_partner
from app.compliance.interface import ComplianceProvider
from app.compliance.mock_digital_sky import MockDigitalSky
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.db.uow import SqlAlchemyUnitOfWork
from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.link import DroneLink
from app.dispatch.mqtt_link import MqttDroneLink
from app.dispatch.service import DispatchService
from app.logging import configure_logging
from app.mqtt.client import MqttClient
from app.realtime.connection import connect
from app.realtime.hub import Hub
from app.realtime.memory import InMemoryRealtime
from app.realtime.notifier import AuditBroadcaster
from app.realtime.ports import LiveView
from app.realtime.redis_backend import RedisRealtime
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory

logger = structlog.get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[None]]


async def _always_ready() -> None:
    return None


def _database_check(engine: AsyncEngine) -> ReadinessCheck:
    async def check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return check


def create_app(
    *,
    settings: Settings | None = None,
    uow_factory: UnitOfWorkFactory | None = None,
    registry: AdapterRegistry | None = None,
    readiness_check: ReadinessCheck | None = None,
    compliance: ComplianceProvider | None = None,
    drone_link: DroneLink | None = None,
    mqtt_client: MqttClient | None = None,
    realtime: LiveView | None = None,
) -> FastAPI:
    """Build the application.

    With no ``uow_factory`` the app connects to PostgreSQL using ``settings`` when it starts
    up (lifespan). Passing a ``uow_factory`` (for example the in-memory one) skips the
    database entirely, which is how the API tests run.

    ``compliance`` defaults to the mock Digital Sky (the only provider that exists). The drone
    link follows ``settings.drone_link``: the in-memory stand-in, or MQTT, in which case the
    app runs an MQTT client for the acknowledgements and reports not-ready while it is not
    connected. Tests pass their own ``compliance``, ``drone_link`` or ``mqtt_client``.

    ``realtime`` is the live view (Redis, from ``settings.redis_url``, or a test stand-in):
    optional, a view and never the source of truth. Without it everything still works; with
    it, committed changes are announced to watchers and ``GET /internal/alerts`` has content.
    """
    settings = settings or get_settings()
    compliance = compliance or MockDigitalSky(
        validity=timedelta(minutes=settings.permission_validity_minutes)
    )
    mqtt_link: MqttDroneLink | None = None
    if drone_link is None:
        if settings.drone_link == "mqtt":
            instance_id = f"api-{uuid4().hex[:12]}"
            mqtt_client = mqtt_client or MqttClient(settings.mqtt_url, client_id=instance_id)
            mqtt_link = MqttDroneLink(
                mqtt_client,
                instance_id=instance_id,
                ack_timeout_s=settings.mqtt_ack_timeout_seconds,
            )
            drone_link = mqtt_link
        else:
            drone_link = InMemoryDroneLink()
    link_client = mqtt_client if mqtt_link is not None else None

    def with_mqtt(check: ReadinessCheck) -> ReadinessCheck:
        """Not ready while the MQTT link has no connection: commands could not be sent."""
        if link_client is None:
            return check

        async def combined() -> None:
            await check()
            if not link_client.connected:
                raise RuntimeError("not connected to the MQTT broker")

        return combined

    redis_client = None
    if realtime is None and settings.redis_url:
        redis_client = connect(settings.redis_url)
        realtime = RedisRealtime(
            redis_client, state_ttl=timedelta(seconds=settings.live_state_ttl_seconds)
        )
    notifier = AuditBroadcaster(realtime) if realtime is not None else None
    hub = Hub(max_queue=settings.ws_queue_size)
    if isinstance(realtime, InMemoryRealtime):
        hub.attach_memory(realtime.listeners)

    def wire(app: FastAPI, factory: UnitOfWorkFactory) -> None:
        orders = OrderService(factory, notifier=notifier)
        app.state.order_service = orders
        app.state.compliance = compliance
        app.state.drone_link = drone_link
        app.state.realtime = realtime
        app.state.dispatch_service = DispatchService(
            factory, orders=orders, compliance=compliance, link=drone_link, notifier=notifier
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level, json_output=settings.log_json)
        engine: AsyncEngine | None = None
        if uow_factory is None:
            engine = create_engine_from_settings(settings)
            session_factory = create_session_factory(engine)
            wire(app, lambda: SqlAlchemyUnitOfWork(session_factory))
            app.state.readiness_check = with_mqtt(readiness_check or _database_check(engine))
        feed_task: asyncio.Task[None] | None = None
        if isinstance(realtime, RedisRealtime):
            feed_task = asyncio.create_task(hub.run_redis(realtime))
        mqtt_task: asyncio.Task[None] | None = None
        if mqtt_link is not None and link_client is not None:
            mqtt_task = asyncio.create_task(
                link_client.run({mqtt_link.reply_topic: mqtt_link.handle_ack})
            )
        logger.info("app_started", clients=app.state.adapter_registry.client_ids())
        try:
            yield
        finally:
            if feed_task is not None:
                feed_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feed_task
            if mqtt_task is not None:
                mqtt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await mqtt_task
            if redis_client is not None:
                await redis_client.aclose()
            if engine is not None:
                await engine.dispose()
            logger.info("app_stopped")

    app = FastAPI(title="Drone Delivery Platform", version="0.1.0", lifespan=lifespan)
    app.state.adapter_registry = registry or default_registry()
    app.state.readiness_check = with_mqtt(readiness_check or _always_ready)
    app.state.hub = hub
    app.state.ws_ping_s = settings.ws_ping_seconds
    app.state.realtime = realtime
    if uow_factory is not None:
        wire(app, uow_factory)

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(partner.router)
    app.include_router(internal_orders.router)
    app.include_router(internal_drones.router)
    app.include_router(internal_flights.router)
    app.include_router(internal_alerts.router)
    app.include_router(ws_internal.router)
    app.include_router(ws_partner.router)
    return app
