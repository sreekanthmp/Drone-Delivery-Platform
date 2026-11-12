"""Liveness and readiness probes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan (startup/shutdown) and give back a client for it."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as http:
            yield http


async def test_health_is_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_dependencies_are_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_not_ready_when_a_dependency_check_fails() -> None:
    async def failing() -> None:
        raise ConnectionError("db.internal:5432 refused the connection")

    app = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(InMemoryDatabase()),
        readiness_check=failing,
    )
    async with serving(app) as http:
        response = await http.get("/ready")
        health = await http.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "db.internal" not in response.text, "no internal details in the probe response"
    assert health.status_code == 200, "liveness does not depend on dependencies"


async def test_the_app_starts_and_reports_not_ready_when_the_database_is_unreachable() -> None:
    """Startup must not crash without a database; readiness is what reports the problem."""
    app = create_app(
        settings=Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        )
    )

    async with serving(app) as http:
        assert (await http.get("/health")).status_code == 200
        response = await http.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
