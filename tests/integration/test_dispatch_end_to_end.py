"""Dispatch through the whole stack: HTTP -> adapter -> services -> PostgreSQL, and back.

The app is built the way production builds it (settings, composition root, the lifespan
creating the engine), so this also proves the defaults are coherent: the default permission
validity must outlast the mission the planner produces, or no dispatch could ever start.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.link import NoAcknowledgementError
from app.main import create_app
from tests.api.conftest import DRONE_BODY, create_path, lookup_path, place_order, register_drone
from tests.integration.conftest import RunSql
from tests.payloads import VALID_PAYLOADS

pytestmark = pytest.mark.integration


@pytest.fixture
async def real_app(database_url: str) -> AsyncIterator[FastAPI]:
    app = create_app(settings=Settings(_env_file=None, database_url=database_url))  # type: ignore[call-arg]
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http(real_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=real_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as client:
        yield client


@pytest.fixture
def link(real_app: FastAPI) -> InMemoryDroneLink:
    drone_link: InMemoryDroneLink = real_app.state.drone_link
    return drone_link


async def trail(run_sql: RunSql, order_id: Any) -> list[tuple[str, str]]:
    rows = await run_sql(
        "SELECT event_type, new_state FROM audit_events WHERE order_id = :o ORDER BY seq",
        o=order_id,
    )
    return [(row[0], row[1]) for row in rows]


async def test_a_partner_order_is_dispatched_flown_and_audited(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    drone = await register_drone(http)
    order_id = await place_order(http, "swiggy")

    dispatched = await http.post(f"/internal/orders/{order_id}/dispatch")

    assert dispatched.status_code == 200
    body = dispatched.json()
    assert body["outcome"] == "started"
    assert body["order"]["status"] == "assigned"
    assert body["drone"]["status"] == "on_mission"
    flight = body["flight"]

    # what is in the database
    flight_row = (
        await run_sql("SELECT status, drone_id, acknowledged_checksum, permission_id FROM flights")
    )[0]
    assert flight_row[0] == "started"
    assert str(flight_row[1]) == drone["id"]
    assert flight_row[2] == flight["mission"]["checksum"]
    assert flight_row[3] == flight["permission"]["permission_id"]
    assert (await run_sql("SELECT status FROM drones"))[0][0] == "on_mission"
    assert (await run_sql("SELECT status FROM orders"))[0][0] == "assigned"

    # the Rule 2 order of events, recorded atomically with each change
    assert await trail(run_sql, order_id) == [
        ("order_created", "created"),
        ("flight_created", "planned"),
        ("flight_status_changed", "permission_granted"),
        ("flight_status_changed", "mission_uploaded"),
        ("order_status_changed", "assigned"),
        ("flight_status_changed", "started"),
    ]

    # the drone got a mission and a start, nothing else (Rule 1), and holds the planned mission
    assert [op for op, _ in link.commands] == ["upload", "start"]
    assert link.on_board[UUID(drone["id"])].checksum == flight["mission"]["checksum"]


async def test_the_default_settings_grant_a_permission_that_covers_the_mission(
    http: httpx.AsyncClient,
) -> None:
    await register_drone(http)
    order_id = await place_order(http)

    flight = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()["flight"]

    granted = datetime.fromisoformat(flight["permission"]["valid_from"])
    until = datetime.fromisoformat(flight["permission"]["valid_until"])
    assert (until - granted).total_seconds() > flight["mission"]["estimated_duration_s"]


async def test_the_commands_and_a_recall_are_recorded_in_postgres(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    await register_drone(http)
    order_id = await place_order(http)
    flight_id = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()["flight"]["id"]

    assert (await http.post(f"/internal/flights/{flight_id}/pause")).json()["status"] == "paused"
    assert (await http.post(f"/internal/flights/{flight_id}/resume")).json()["status"] == "started"
    recalled = await http.post(
        f"/internal/flights/{flight_id}/return-to-home", json={"reason": "weather"}
    )

    assert recalled.status_code == 200
    assert recalled.json()["status"] == "aborted"
    assert (await run_sql("SELECT status FROM flights"))[0][0] == "aborted"
    assert (await run_sql("SELECT status FROM drones"))[0][0] == "returning"
    assert [op for op, _ in link.commands] == [
        "upload",
        "start",
        "pause",
        "start",
        "return_to_home",
    ]
    assert [state for _, state in await trail(run_sql, order_id)][-4:] == [
        "started",
        "paused",
        "started",
        "aborted",
    ]


async def test_a_partner_cancel_recalls_a_flying_drone_and_cancels_the_order(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    await register_drone(http)
    order_id = await place_order(http, "zomato")
    await http.post(f"/internal/orders/{order_id}/dispatch")

    response = await http.post("/partner/zomato/orders/99812/cancel")

    assert response.status_code == 200
    assert response.json()["order"]["state"] == "cancelled"
    assert [op for op, _ in link.commands] == ["upload", "start", "return_to_home"]
    assert (await run_sql("SELECT status FROM orders"))[0][0] == "cancelled"
    assert (await run_sql("SELECT status FROM flights"))[0][0] == "aborted"
    assert (await trail(run_sql, order_id))[-2:] == [
        ("flight_status_changed", "aborted"),
        ("order_status_changed", "cancelled"),
    ]


async def test_a_recall_that_cannot_be_confirmed_leaves_the_order_flying_and_retryable(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    await register_drone(http)
    order_id = await place_order(http, "mock")
    await http.post(f"/internal/orders/{order_id}/dispatch")
    link.set_failure("return_to_home", NoAcknowledgementError("no answer"))

    refused = await http.post("/partner/mock/orders/ORD-1/cancel")

    assert refused.status_code == 503
    assert (await run_sql("SELECT status FROM orders"))[0][0] == "assigned"
    assert (await run_sql("SELECT status FROM flights"))[0][0] == "started"

    link.set_failure("return_to_home", None)
    assert (await http.post("/partner/mock/orders/ORD-1/cancel")).status_code == 200
    assert (await run_sql("SELECT status FROM orders"))[0][0] == "cancelled"


async def test_ten_concurrent_dispatch_requests_start_one_flight(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    for i in range(3):
        await register_drone(http, name=f"drone-{i}")
    order_id = await place_order(http)

    responses = await asyncio.gather(
        *(http.post(f"/internal/orders/{order_id}/dispatch") for _ in range(10))
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200] + [409] * 9
    assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 1
    assert [op for op, _ in link.commands] == ["upload", "start"]
    assert (await run_sql("SELECT count(*) FROM drones WHERE status <> 'available'"))[0][0] == 1


async def test_many_orders_share_a_small_fleet_without_double_booking(
    http: httpx.AsyncClient, link: InMemoryDroneLink, run_sql: RunSql
) -> None:
    for i in range(3):
        await register_drone(http, name=f"drone-{i}")
    order_ids = []
    for i in range(9):
        response = await http.post(
            create_path("mock"), json={**VALID_PAYLOADS["mock"](), "order_id": f"ORD-{i}"}
        )
        order_ids.append(response.json()["tracking_id"])

    responses = await asyncio.gather(
        *(http.post(f"/internal/orders/{order_id}/dispatch") for order_id in order_ids)
    )

    outcomes = [r.json()["outcome"] for r in responses]
    assert outcomes.count("started") == 3
    assert outcomes.count("no_drone_available") == 6
    assert (await run_sql("SELECT count(DISTINCT drone_id) FROM flights"))[0][0] == 3
    assert (await run_sql("SELECT count(*) FROM flights WHERE status = 'started'"))[0][0] == 3
    assert len(link.operations("start")) == 3 == len(set(link.operations("start")))
    # the unassigned orders are untouched and can be dispatched later
    assert (await run_sql("SELECT count(*) FROM orders WHERE status = 'created'"))[0][0] == 6


async def test_a_dispatch_leaves_the_partner_view_consistent(http: httpx.AsyncClient) -> None:
    await register_drone(http)
    order_id = await place_order(http, "swiggy")
    await http.post(f"/internal/orders/{order_id}/dispatch")

    looked_up = await http.get(lookup_path("swiggy"))

    assert looked_up.json()["data"]["order_status"] == "DRONE_ASSIGNED"


async def test_the_drone_registration_body_matches_what_is_stored(
    http: httpx.AsyncClient, run_sql: RunSql
) -> None:
    drone = await register_drone(http)

    row = (
        await run_sql(
            "SELECT name, status, max_payload_kg, max_range_km, battery_percent,"
            " ST_X(home::geometry), ST_Y(home::geometry) FROM drones"
        )
    )[0]

    assert row[0] == DRONE_BODY["name"] == drone["name"]
    assert row[1] == "available"
    assert (row[2], row[3], row[4]) == (5.0, 20.0, 90.0)
    assert (row[5], row[6]) == (77.6, 12.98)  # PostGIS order: longitude, latitude
