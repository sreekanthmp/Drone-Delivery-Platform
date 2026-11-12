"""Application factory and composition root.

Run with::

    uvicorn app.main:create_app --factory

This is the one place that wires the layers together (settings, database, adapters,
services, routers), so it is the only module allowed to import across all of them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.registry import AdapterRegistry, default_registry
from app.api import health
from app.api.errors import register_error_handlers
from app.api.internal import orders as internal_orders
from app.api.partner import router as partner
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.db.uow import SqlAlchemyUnitOfWork
from app.logging import configure_logging
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
) -> FastAPI:
    """Build the application.

    With no ``uow_factory`` the app connects to PostgreSQL using ``settings`` when it starts
    up (lifespan). Passing a ``uow_factory`` (for example the in-memory one) skips the
    database entirely, which is how the API tests run.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=settings.log_level, json_output=settings.log_json)
        engine: AsyncEngine | None = None
        if uow_factory is None:
            engine = create_engine_from_settings(settings)
            session_factory = create_session_factory(engine)
            app.state.order_service = OrderService(lambda: SqlAlchemyUnitOfWork(session_factory))
            app.state.readiness_check = readiness_check or _database_check(engine)
        logger.info("app_started", clients=app.state.adapter_registry.client_ids())
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()
            logger.info("app_stopped")

    app = FastAPI(title="Drone Delivery Platform", version="0.1.0", lifespan=lifespan)
    app.state.adapter_registry = registry or default_registry()
    app.state.readiness_check = readiness_check or _always_ready
    if uow_factory is not None:
        app.state.order_service = OrderService(uow_factory)

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(partner.router)
    app.include_router(internal_orders.router)
    return app
