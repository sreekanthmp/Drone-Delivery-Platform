"""The live view through the API: ``/internal/alerts``, and announcements of committed changes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.realtime import channels
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Alert, AlertKind
from tests.api.conftest import place_order, register_drone
from tests.conftest import DRONE_ID, FLIGHT_ID, ORDER_ID, T0
from tests.dispatch_rig import ScriptedCompliance
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork


@pytest.fixture
def live() -> InMemoryRealtime:
    return InMemoryRealtime()


@pytest.fixture
async def http(live: InMemoryRealtime) -> AsyncIterator[httpx.AsyncClient]:
    database = InMemoryDatabase()  # one database for the whole app, not one per transaction
    app: FastAPI = create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(database),
        compliance=ScriptedCompliance(validity=timedelta(hours=1)),
        realtime=live,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
    ) as client:
        yield client


def alert(kind: AlertKind = AlertKind.LINK_LOST, **kwargs: object) -> Alert:
    return Alert(
        kind=kind,
        drone_id=DRONE_ID,
        flight_id=FLIGHT_ID,
        order_id=ORDER_ID,
        message="no telemetry for 20 s",
        raised_at=T0,
        **kwargs,  # type: ignore[arg-type]
    )


class TestAlertsEndpoint:
    async def test_without_a_live_view_it_says_so_and_is_empty(self) -> None:
        database = InMemoryDatabase()
        app = create_app(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            uow_factory=lambda: InMemoryUnitOfWork(database),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
        ) as client:
            response = await client.get("/internal/alerts")

        assert response.status_code == 200
        assert response.json() == {"live": False, "active": [], "recent": []}

    async def test_standing_and_recent_alerts(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        await live.raise_alert(alert(AlertKind.LINK_LOST))
        await live.raise_alert(alert(AlertKind.LOW_BATTERY))
        await live.resolve(AlertKind.LOW_BATTERY, DRONE_ID, at=T0 + timedelta(seconds=9))
        await live.notify(alert(AlertKind.FAULT))

        body = (await http.get("/internal/alerts")).json()

        assert body["live"] is True
        assert [a["kind"] for a in body["active"]] == ["link_lost"]
        assert [(a["kind"], a["resolved_at"] is not None) for a in body["recent"]] == [
            ("fault", False),
            ("low_battery", True),
            ("low_battery", False),
            ("link_lost", False),
        ]
        first = body["active"][0]
        assert first["drone_id"] == str(DRONE_ID)
        assert first["flight_id"] == str(FLIGHT_ID)
        assert first["order_id"] == str(ORDER_ID)
        assert first["message"] == "no telemetry for 20 s"

    async def test_the_limit_applies_to_the_recent_list(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        for _ in range(5):
            await live.notify(alert(AlertKind.FAULT))

        body = (await http.get("/internal/alerts", params={"limit": 2})).json()

        assert len(body["recent"]) == 2

    @pytest.mark.parametrize("limit", [0, 201, -1])
    async def test_a_silly_limit_is_refused(self, http: httpx.AsyncClient, limit: int) -> None:
        assert (await http.get("/internal/alerts", params={"limit": limit})).status_code == 422

    async def test_an_unreachable_live_view_is_a_503_not_a_crash(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        live.broken = True

        response = await http.get("/internal/alerts")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "live_view_unavailable"

    async def test_it_is_read_only(self, http: httpx.AsyncClient) -> None:
        for method in ("post", "put", "patch", "delete"):
            response = await http.request(method, "/internal/alerts")
            assert response.status_code == 405


class TestChangesAreAnnounced:
    async def test_a_dispatch_through_http_is_announced_on_the_order_and_fleet_channels(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        await register_drone(http)
        order_id = await place_order(http)
        await http.post(f"/internal/orders/{order_id}/dispatch")

        order_events = [
            e.data["new_state"]
            for channel, e in live.published
            if channel == channels.order_channel(order_id)
        ]
        fleet_events = [e for channel, e in live.published if channel == channels.FLEET]

        assert order_events == [
            "created",
            "planned",
            "permission_granted",
            "mission_uploaded",
            "assigned",
            "started",
        ]
        assert len(fleet_events) == 6
        assert {e.kind for e in fleet_events} == {"audit"}

    async def test_a_partner_cancel_is_announced_too(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        order_id = await place_order(http)

        await http.post("/partner/mock/orders/ORD-1/cancel")

        states = [
            e.data["new_state"]
            for channel, e in live.published
            if channel == channels.order_channel(order_id)
        ]
        assert states == ["created", "cancelled"]

    async def test_a_dead_live_view_does_not_stop_work(
        self, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        live.broken = True
        await register_drone(http)
        order_id = await place_order(http)

        body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "started"
        assert (await http.get(f"/internal/orders/{order_id}")).json()["status"] == "assigned"
        assert live.published == []
