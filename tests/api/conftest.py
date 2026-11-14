"""API test fixtures: the real FastAPI app on top of the in-memory unit of work (no database)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.dispatch.service import DispatchService
from app.main import create_app
from app.services.order_service import OrderService
from tests.dispatch_rig import HookedLink, ScriptedCompliance
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork
from tests.payloads import EXTERNAL_IDS, VALID_PAYLOADS

# How each partner's response exposes our tracking reference, status and own id.
PLATFORM_REF: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda body: body["tracking_id"],
    "swiggy": lambda body: body["data"]["drone_order_ref"],
    "zomato": lambda body: body["order"]["platform_ref"],
}
PARTNER_STATUS: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda body: body["status"],
    "swiggy": lambda body: body["data"]["order_status"],
    "zomato": lambda body: body["order"]["state"],
}
CLIENTS = sorted(VALID_PAYLOADS)


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def compliance() -> ScriptedCompliance:
    return ScriptedCompliance(validity=timedelta(hours=1))


@pytest.fixture
def link() -> HookedLink:
    return HookedLink()


@pytest.fixture
def app(database: InMemoryDatabase, compliance: ScriptedCompliance, link: HookedLink) -> FastAPI:
    return create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(database),
        compliance=compliance,
        drone_link=link,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as http:
        yield http


@pytest.fixture
def service(app: FastAPI) -> OrderService:
    service: OrderService = app.state.order_service
    return service


@pytest.fixture
def dispatch(app: FastAPI) -> DispatchService:
    service: DispatchService = app.state.dispatch_service
    return service


DRONE_BODY: dict[str, Any] = {
    "name": "drone-1",
    "home": {"latitude": 12.98, "longitude": 77.60},
    "max_payload_kg": 5.0,
    "max_range_km": 20.0,
    "battery_percent": 90.0,
}


async def register_drone(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    """Register a drone through the API and return its representation."""
    response = await client.post("/internal/drones", json={**DRONE_BODY, **overrides})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def place_order(client: httpx.AsyncClient, client_id: str = "mock") -> UUID:
    """Create an order through a partner adapter and return its internal id."""
    response = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())
    assert response.status_code == 201, response.text
    return UUID(PLATFORM_REF[client_id](response.json()))


def create_path(client_id: str) -> str:
    return f"/partner/{client_id}/orders"


def lookup_path(client_id: str) -> str:
    return f"/partner/{client_id}/orders/{EXTERNAL_IDS[client_id]}"
