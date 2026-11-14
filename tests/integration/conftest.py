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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

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
from tests.conftest import DRONE_ID, build_drone
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

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


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def uow_factory(request: pytest.FixtureRequest) -> UnitOfWorkFactory:
    """The same tests against the in-memory fake and the real PostgreSQL implementation."""
    if request.param == "memory":
        database = InMemoryDatabase()
        return lambda: InMemoryUnitOfWork(database)
    factory: UnitOfWorkFactory = request.getfixturevalue("pg_uow_factory")
    return factory


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


RegisterDrone = Callable[..., Awaitable[UUID]]


@pytest.fixture
def register_drone(pg_uow_factory: UnitOfWorkFactory) -> RegisterDrone:
    """Register a drone in the database (orders may only reference registered drones)."""

    async def _register(drone_id: UUID = DRONE_ID, **overrides: Any) -> UUID:
        overrides.setdefault("name", f"drone-{drone_id.hex[-12:]}")
        async with pg_uow_factory() as uow:
            assert await uow.drones.add(build_drone(id=drone_id, **overrides))
            await uow.commit()
        return drone_id

    return _register


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


# --- MQTT: a real Mosquitto broker -----------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def mosquitto_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway Mosquitto broker on a free port (anonymous, localhost only).

    Skipped with the reason when the binary is missing, unless ``REQUIRE_INTEGRATION=1``.
    """
    import shutil
    import socket
    import subprocess
    import time

    binary = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
    if not Path(binary).exists():
        _unavailable("the mosquitto binary is not installed")
        return
    port = _free_port()
    config = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    process = subprocess.Popen(
        [binary, "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    _unavailable("the mosquitto broker did not start")
                    return
                time.sleep(0.05)
        yield f"mqtt://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


# --- Redis: a real redis-server --------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A throwaway Redis on a free port (no persistence, localhost only).

    Skipped with the reason when the binary is missing, unless ``REQUIRE_INTEGRATION=1``.
    """
    import shutil
    import socket
    import subprocess
    import time

    binary = shutil.which("redis-server")
    if binary is None:
        _unavailable("the redis-server binary is not installed")
        return
    port = _free_port()
    process = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    _unavailable("the redis server did not start")
                    return
                time.sleep(0.05)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    """A client on a clean Redis database."""
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
