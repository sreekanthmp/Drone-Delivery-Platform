"""The whole stack: HTTP -> adapter -> service -> PostgreSQL/PostGIS, and back."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.domain.models import EventSource, OrderStatus
from app.main import create_app
from app.services.order_service import OrderService
from tests.api.conftest import PARTNER_STATUS, PLATFORM_REF, create_path, lookup_path
from tests.integration.conftest import RegisterDrone, RunSql
from tests.payloads import VALID_PAYLOADS, set_path

pytestmark = pytest.mark.integration

S = OrderStatus


@pytest.fixture
async def real_app(database_url: str) -> AsyncIterator[FastAPI]:
    """The app wired to a real database; the lifespan builds and disposes the engine."""
    app = create_app(settings=Settings(_env_file=None, database_url=database_url))  # type: ignore[call-arg]
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http(real_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=real_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as client:
        yield client


async def test_readiness_checks_the_real_database(http: httpx.AsyncClient) -> None:
    response = await http.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_swiggy_order_lands_in_postgres_with_the_right_coordinates(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    response = await http.post(create_path("swiggy"), json=VALID_PAYLOADS["swiggy"]())

    assert response.status_code == 201
    row = (
        await run_sql(
            "SELECT client_id, external_order_id, status, package_weight_kg,"
            " ST_X(pickup::geometry), ST_Y(pickup::geometry),"
            " ST_X(dropoff::geometry), ST_Y(dropoff::geometry) FROM orders"
        )
    )[0]
    assert row[:3] == ("swiggy", "SW-100234", "created")
    assert row[3] == pytest.approx(1.1)
    # PostGIS stores (longitude, latitude): restaurant 77.5946/12.9716, customer 77.6245/12.9352.
    assert (row[4], row[5]) == (77.5946, 12.9716)
    assert (row[6], row[7]) == (77.6245, 12.9352)


async def test_zomato_geojson_order_is_stored_without_swapping_coordinates(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    """Zomato sends [lon, lat]. The adapter must hand the domain (lat, lon) so PostGIS ends up
    with x = longitude, y = latitude, the same place the partner meant."""
    await http.post(create_path("zomato"), json=VALID_PAYLOADS["zomato"]())

    row = (await run_sql("SELECT ST_X(pickup::geometry), ST_Y(pickup::geometry) FROM orders"))[0]
    assert (row[0], row[1]) == (77.5946, 12.9716)


async def test_creation_is_audited_as_an_adapter_event(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    await http.post(create_path("mock"), json=VALID_PAYLOADS["mock"]())

    row = (
        await run_sql(
            "SELECT event_type, previous_state, new_state, source, metadata::text FROM audit_events"
        )
    )[0]
    assert row[:4] == ("order_created", None, "created", "adapter")
    assert '"client_id": "mock"' in row[4]


async def test_the_full_flow_reflects_status_changes_in_each_partners_vocabulary(
    http: httpx.AsyncClient, real_app: FastAPI, register_drone: RegisterDrone
) -> None:
    drone = await register_drone(UUID(int=0xD0))
    service: OrderService = real_app.state.order_service
    expected = {
        "mock": ["created", "assigned", "in_flight"],
        "swiggy": ["PLACED", "DRONE_ASSIGNED", "OUT_FOR_DELIVERY"],
        "zomato": ["accepted", "dispatch_assigned", "in_transit"],
    }

    for client_id, names in expected.items():
        created = await http.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())
        order_id = UUID(PLATFORM_REF[client_id](created.json()))
        seen = [PARTNER_STATUS[client_id]((await http.get(lookup_path(client_id))).json())]

        await service.transition(order_id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=drone)
        seen.append(PARTNER_STATUS[client_id]((await http.get(lookup_path(client_id))).json()))
        await service.transition(order_id, S.PICKED_UP, source=EventSource.DISPATCH)
        await service.transition(order_id, S.IN_FLIGHT, source=EventSource.TELEMETRY)
        seen.append(PARTNER_STATUS[client_id]((await http.get(lookup_path(client_id))).json()))

        assert seen == names, client_id


async def test_ten_concurrent_identical_posts_create_exactly_one_order(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    payload = VALID_PAYLOADS["swiggy"]()

    responses = await asyncio.gather(
        *(http.post(create_path("swiggy"), json=payload) for _ in range(10))
    )

    assert sorted(r.status_code for r in responses) == [200] * 9 + [201]
    assert len({PLATFORM_REF["swiggy"](r.json()) for r in responses}) == 1
    assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1
    assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 1


async def test_a_payload_that_fails_validation_leaves_the_database_untouched(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    bad = set_path(VALID_PAYLOADS["swiggy"](), "restaurant.location.latitude", 123)

    response = await http.post(create_path("swiggy"), json=bad)

    assert response.status_code == 422
    assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 0
    assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 0


async def test_the_internal_api_reads_the_same_order_from_postgres(
    http: httpx.AsyncClient,
) -> None:
    created = await http.post(create_path("zomato"), json=VALID_PAYLOADS["zomato"]())
    order_id = PLATFORM_REF["zomato"](created.json())

    order = (await http.get(f"/internal/orders/{order_id}")).json()
    status = (await http.get(f"/internal/orders/{order_id}/status")).json()

    assert order["client_id"] == "zomato"
    assert order["external_order_id"] == "99812"
    assert order["pickup"] == {"latitude": 12.9716, "longitude": 77.5946}
    assert order["package"]["weight_kg"] == pytest.approx(1.2)
    assert status == {
        "order_id": order_id,
        "status": "created",
        "is_terminal": False,
        "updated_at": order["updated_at"],
    }
