"""API test fixtures: the real FastAPI app on top of the in-memory unit of work (no database)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.services.order_service import OrderService
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
def app(database: InMemoryDatabase) -> FastAPI:
    return create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(database),
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


def create_path(client_id: str) -> str:
    return f"/partner/{client_id}/orders"


def lookup_path(client_id: str) -> str:
    return f"/partner/{client_id}/orders/{EXTERNAL_IDS[client_id]}"
