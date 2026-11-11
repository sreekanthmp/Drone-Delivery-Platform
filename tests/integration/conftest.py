"""Fixtures for tests that need a real PostgreSQL with PostGIS.

How isolation works
-------------------
A session-scoped *template* database is created once and migrated with Alembic.
Every test then gets its own database cloned from that template and dropped
afterwards. Truncating tables between tests is impossible by design (audit_events
rejects TRUNCATE), and rolling back a wrapper transaction would never fire the
commit-time lifecycle trigger, so real databases per test are the honest option.

If PostgreSQL/PostGIS is unreachable the tests are skipped with the reason, unless
``REQUIRE_INTEGRATION=1`` is set, in which case they fail (use that in CI).

All fixtures are synchronous and use ``NullPool`` so nothing is tied to a
particular event loop.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Row, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.db.session import create_engine, create_session_factory
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.models import EventSource, Location, Order, Package
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_ADMIN_URL", "postgresql://drone:drone@localhost:5432/postgres"
)


def _unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_INTEGRATION") == "1":
        pytest.fail(f"REQUIRE_INTEGRATION=1 but infrastructure is unavailable: {reason}")
    pytest.skip(f"integration infrastructure unavailable: {reason}")


async def _admin(sql: str) -> Any:
    connection = await asyncpg.connect(ADMIN_DSN, timeout=5)
    try:
        return await connection.fetchval(sql)
    finally:
        await connection.close()


def sqlalchemy_url(database: str) -> str:
    url = make_url(ADMIN_DSN).set(drivername="postgresql+asyncpg", database=database)
    return url.render_as_string(hide_password=False)


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated_template() -> Iterator[str]:
    template = f"drone_tpl_{os.getpid()}"
    try:
        postgis = asyncio.run(
            _admin("SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'")
        )
    except Exception as exc:
        _unavailable(f"cannot connect to PostgreSQL ({type(exc).__name__}: {exc})")
        return
    if not postgis:
        _unavailable("the PostGIS extension is not installed on the PostgreSQL server")

    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{template}"'))
    try:
        command.upgrade(alembic_config(sqlalchemy_url(template)), "head")
        yield template
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)'))


@pytest.fixture
def database_url(migrated_template: str) -> Iterator[str]:
    name = f"drone_t_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"'))
    try:
        yield sqlalchemy_url(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def engine(database_url: str) -> AsyncEngine:
    return create_engine(database_url, poolclass=NullPool)


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def pg_uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    return lambda: SqlAlchemyUnitOfWork(session_factory)


@pytest.fixture
def pg_service(pg_uow_factory: UnitOfWorkFactory) -> OrderService:
    return OrderService(pg_uow_factory)


@pytest.fixture
async def created_order(
    pg_service: OrderService, pickup: Location, dropoff: Location, package: Package
) -> Order:
    """An order stored through the proper path, in status ``created``."""
    result = await pg_service.create_order(
        client_id="mock",
        external_order_id="FIXTURE-1",
        pickup=pickup,
        dropoff=dropoff,
        package=package,
        source=EventSource.API,
    )
    return result.order


RunTx = Callable[..., Awaitable[None]]


@pytest.fixture
def run_tx(engine: AsyncEngine) -> RunTx:
    """Run several raw ``(sql, params)`` statements in ONE transaction.

    The lifecycle trigger is deferred, so it fires when this transaction commits.
    """

    async def _run(*statements: tuple[str, dict[str, Any]]) -> None:
        async with engine.begin() as connection:
            for statement, params in statements:
                await connection.execute(text(statement), params)

    return _run


RunSql = Callable[..., Awaitable[list[Row[Any]]]]


@pytest.fixture
def run_sql(engine: AsyncEngine) -> RunSql:
    """Run raw SQL in its own transaction (committed on success) and return the rows."""

    async def _run(statement: str, **params: Any) -> list[Row[Any]]:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []

    return _run
