"""The live WebSocket streams, over real sockets (platform streams and the partner stream)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.domain.models import EventSource, OrderStatus
from app.main import create_app
from app.protocol.messages import DronePhase
from app.realtime.channels import FLEET, drone_channel
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Alert, AlertKind, DroneState, Envelope
from tests.api.conftest import CLIENTS, place_order, register_drone
from tests.api.live_server import (
    close_code,
    connect,
    frame,
    frames_until,
    quiet,
    serve,
)
from tests.dispatch_rig import HookedLink, ScriptedCompliance
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork
from tests.payloads import EXTERNAL_IDS

S = OrderStatus
PARTNER_STATUS: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda b: b["status"],
    "swiggy": lambda b: b["data"]["order_status"],
    "zomato": lambda b: b["order"]["state"],
}
PARTNER_POSITION: dict[str, Callable[[dict[str, Any]], Any]] = {
    "mock": lambda b: b["position"],
    "swiggy": lambda b: b["data"]["live_location"],
    "zomato": lambda b: b["order"]["location"],
}
PARTNER_LATITUDE: dict[str, Callable[[Any], float]] = {
    "mock": lambda p: p["lat"],
    "swiggy": lambda p: p["latitude"],
    "zomato": lambda p: p["coordinates"][1],
}
PARTNER_NOT_FOUND: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda b: b["error"]["code"],
    "swiggy": lambda b: b["error_code"].lower(),
    "zomato": lambda b: b["error"]["type"],
}
IN_FLIGHT_NAME = {"mock": "in_flight", "swiggy": "OUT_FOR_DELIVERY", "zomato": "in_transit"}
ASSIGNED_NAME = {"mock": "assigned", "swiggy": "DRONE_ASSIGNED", "zomato": "dispatch_assigned"}


@pytest.fixture
def live() -> InMemoryRealtime:
    return InMemoryRealtime()


@pytest.fixture
def app(live: InMemoryRealtime) -> FastAPI:
    database = InMemoryDatabase()
    return create_app(
        settings=Settings(_env_file=None, ws_ping_seconds=0.25),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(database),
        compliance=ScriptedCompliance(validity=timedelta(hours=1)),
        drone_link=HookedLink(),
        realtime=live,
    )


@pytest.fixture
async def server(app: FastAPI) -> AsyncIterator[str]:
    async with serve(app) as host:
        yield host


@pytest.fixture
async def http(server: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=f"http://{server}") as client:
        yield client


def sample(
    drone_id: UUID,
    flight_id: UUID | None,
    *,
    latitude: float = 12.95,
    longitude: float = 77.61,
    battery: float = 77.0,
) -> Envelope:
    state = DroneState(
        drone_id=drone_id,
        latitude=latitude,
        longitude=longitude,
        altitude_m=60.0,
        battery_percent=battery,
        phase=DronePhase.FLYING,
        flight_id=flight_id,
        sent_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
    )
    return Envelope(kind="drone_state", droppable=True, data=state.model_dump(mode="json"))


async def report(live: InMemoryRealtime, envelope: Envelope) -> None:
    """What the worker's recorder does with a sample: remember it, then broadcast it."""
    state = DroneState.model_validate(envelope.data)
    await live.set_state(state)
    await live.publish(drone_channel(state.drone_id), envelope)


async def dispatched(http: httpx.AsyncClient, client_id: str = "mock") -> tuple[UUID, UUID, UUID]:
    """A started flight: ``(order_id, flight_id, drone_id)``."""
    drone = await register_drone(http)
    order_id = await place_order(http, client_id)
    body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()
    assert body["outcome"] == "started"
    return order_id, UUID(body["flight"]["id"]), UUID(drone["id"])


class TestOrderStream:
    async def test_it_starts_with_a_snapshot_of_the_order(
        self, server: str, http: httpx.AsyncClient
    ) -> None:
        order_id = await place_order(http)

        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            snapshot = await frame(ws)

        assert snapshot["type"] == "snapshot"
        assert snapshot["order"]["id"] == str(order_id)
        assert snapshot["order"]["status"] == "created"
        assert snapshot["flight_id"] is None
        assert snapshot["position"] is None

    async def test_committed_changes_arrive_live_in_order(
        self, server: str, http: httpx.AsyncClient
    ) -> None:
        await register_drone(http)
        order_id = await place_order(http)
        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)  # the snapshot

            await http.post(f"/internal/orders/{order_id}/dispatch")
            got = await frames_until(ws, lambda f: len(f) >= 5)

        assert [(f["type"], f["new_state"]) for f in got] == [
            ("audit", "planned"),
            ("audit", "permission_granted"),
            ("audit", "mission_uploaded"),
            ("audit", "assigned"),
            ("audit", "started"),
        ]

    async def test_positions_are_those_of_this_orders_flight_only(
        self, server: str, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        await register_drone(http)
        order_id = await place_order(http)
        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)
            body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()
            flight_id = UUID(body["flight"]["id"])
            drone_id = UUID(body["drone"]["id"])
            await frames_until(ws, lambda f: len(f) >= 5)  # the dispatch's own changes

            await live.publish(drone_channel(drone_id), sample(drone_id, flight_id, latitude=12.91))
            await live.publish(drone_channel(drone_id), sample(drone_id, uuid4()))  # another flight
            await live.publish(drone_channel(drone_id), sample(drone_id, None))  # on the ground
            await live.publish(
                drone_channel(UUID(int=7)),
                sample(UUID(int=7), flight_id),  # another drone
            )
            got = await frames_until(ws, lambda f: len(f) >= 1)
            extra = await quiet(ws)

        assert [f["type"] for f in got] == ["position"]
        assert got[0]["latitude"] == 12.91
        assert got[0]["battery_percent"] == 77.0  # the platform's own stream shows it
        assert got[0]["phase"] == "flying"
        assert extra == []  # nothing else leaked through

    async def test_a_late_joiner_sees_the_current_position_in_its_snapshot(
        self, server: str, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        order_id, flight_id, drone_id = await dispatched(http)
        await live.set_state(
            DroneState.model_validate(sample(drone_id, flight_id, latitude=12.93).data)
        )

        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            snapshot = await frame(ws)

        assert snapshot["flight_id"] == str(flight_id)
        assert snapshot["position"]["latitude"] == 12.93

    async def test_an_unknown_order_gets_an_error_frame_and_a_not_found_close(
        self, server: str
    ) -> None:
        async with connect(server, f"/ws/internal/orders/{uuid4()}") as ws:
            error = await frame(ws)
            code = await close_code(ws)

        assert error["code"] == "not_found"
        assert code == 4404

    async def test_a_quiet_stream_pings(self, server: str, http: httpx.AsyncClient) -> None:
        order_id = await place_order(http)

        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)
            ping = await frame(ws, timeout=2)

        assert ping == {"type": "ping"}

    async def test_whatever_a_client_sends_is_ignored_it_is_a_read_only_stream(
        self, server: str, app: FastAPI, http: httpx.AsyncClient
    ) -> None:
        order_id, _, _ = await dispatched(http)
        link: HookedLink = app.state.drone_link
        sent = list(link.commands)
        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)

            for attempt in (
                {"command": "return_to_home"},
                {"type": "land"},
                {"latitude": 1.0, "longitude": 2.0},
                {"status": "delivered"},
            ):
                await ws.send(json.dumps(attempt))
            await ws.send(b"\x00\x01binary")
            await ws.send("not even json")
            replies = await quiet(ws)

        assert replies == []
        assert link.commands == sent  # no command reached a drone
        assert (await http.get(f"/internal/orders/{order_id}")).json()["status"] == "assigned"
        flights = (await http.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["started"]

    async def test_a_disconnected_client_is_forgotten(
        self, server: str, app: FastAPI, http: httpx.AsyncClient
    ) -> None:
        order_id = await place_order(http)
        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)
            assert app.state.hub.watchers == 1

        for _ in range(100):
            if app.state.hub.watchers == 0:
                break
            await asyncio.sleep(0.02)
        assert app.state.hub.watchers == 0

    async def test_a_feed_interruption_tells_watchers_to_reconnect_and_resync(
        self, server: str, app: FastAPI, http: httpx.AsyncClient
    ) -> None:
        order_id = await place_order(http)
        async with connect(server, f"/ws/internal/orders/{order_id}") as ws:
            await frame(ws)

            app.state.hub.interrupt_all("the live feed was interrupted")
            code = await close_code(ws)

        assert code == 1013


class TestWithoutALiveView:
    async def test_streams_refuse_politely(self) -> None:
        database = InMemoryDatabase()
        app = create_app(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            uow_factory=lambda: InMemoryUnitOfWork(database),
        )
        async with serve(app) as host:
            for path in ("/ws/internal/fleet", f"/ws/internal/orders/{uuid4()}"):
                async with connect(host, path) as ws:
                    error = await frame(ws)
                    code = await close_code(ws)
                assert error["code"] == "live_view_unavailable"
                assert code == 1013


class TestFleetStream:
    async def test_snapshot_lists_the_fleet_with_latest_states_and_standing_alerts(
        self, server: str, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        flying = await register_drone(http, name="alpha")
        await register_drone(http, name="bravo")
        await live.set_state(DroneState.model_validate(sample(UUID(flying["id"]), None).data))
        await live.raise_alert(
            Alert(
                kind=AlertKind.LOW_BATTERY,
                drone_id=UUID(flying["id"]),
                message="battery at 9%",
                raised_at=datetime.now(UTC),
            )
        )

        async with connect(server, "/ws/internal/fleet") as ws:
            snapshot = await frame(ws)

        assert snapshot["type"] == "snapshot"
        by_name = {d["name"]: d for d in snapshot["drones"]}
        assert set(by_name) == {"alpha", "bravo"}
        assert by_name["alpha"]["state"]["battery_percent"] == 77.0
        assert by_name["bravo"]["state"] is None
        assert by_name["alpha"]["status"] == "available"
        assert [a["kind"] for a in snapshot["alerts"]] == ["low_battery"]

    async def test_samples_changes_and_alerts_arrive_live(
        self, server: str, http: httpx.AsyncClient, live: InMemoryRealtime
    ) -> None:
        async with connect(server, "/ws/internal/fleet") as ws:
            await frame(ws)

            drone = await register_drone(http)
            order_id = await place_order(http)
            await http.post(f"/internal/orders/{order_id}/dispatch")
            await live.publish(FLEET, sample(UUID(drone["id"]), None, latitude=12.5))
            await live.raise_alert(
                Alert(
                    kind=AlertKind.LINK_LOST,
                    drone_id=UUID(drone["id"]),
                    message="silent",
                    raised_at=datetime.now(UTC),
                )
            )
            got = await frames_until(
                ws, lambda f: {"audit", "drone_state", "alert"} <= {x["type"] for x in f}
            )

        audits = [f["new_state"] for f in got if f["type"] == "audit"]
        assert audits[:3] == ["created", "planned", "permission_granted"]
        assert [f["latitude"] for f in got if f["type"] == "drone_state"] == [12.5]
        assert [f["kind"] for f in got if f["type"] == "alert"] == ["link_lost"]

    async def test_every_watcher_gets_every_message(
        self, server: str, http: httpx.AsyncClient
    ) -> None:
        async with (
            connect(server, "/ws/internal/fleet") as a,
            connect(server, "/ws/internal/fleet") as b,
        ):
            await frame(a)
            await frame(b)

            await place_order(http)
            first = await frames_until(a, lambda f: len(f) >= 1)
            second = await frames_until(b, lambda f: len(f) >= 1)

        assert first[0]["new_state"] == second[0]["new_state"] == "created"


class TestPartnerStream:
    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_the_snapshot_is_in_the_partners_own_format(
        self, server: str, http: httpx.AsyncClient, client_id: str
    ) -> None:
        await place_order(http, client_id)

        async with connect(server, f"/ws/partner/{client_id}/orders/{_external(client_id)}") as ws:
            snapshot = await frame(ws)

        assert PARTNER_STATUS[client_id](snapshot) in {"created", "PLACED", "accepted"}
        assert PARTNER_POSITION[client_id](snapshot) is None
        assert "type" not in snapshot  # no platform-format envelope: only the partner's shape

    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_status_changes_follow_in_the_partners_vocabulary(
        self, server: str, http: httpx.AsyncClient, client_id: str
    ) -> None:
        await register_drone(http)
        order_id = await place_order(http, client_id)
        async with connect(server, f"/ws/partner/{client_id}/orders/{_external(client_id)}") as ws:
            await frame(ws)

            await http.post(f"/internal/orders/{order_id}/dispatch")
            got = await frames_until(ws, lambda f: len(f) >= 1)

        assert [PARTNER_STATUS[client_id](f) for f in got] == [ASSIGNED_NAME[client_id]]

    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_a_position_appears_only_while_a_drone_carries_the_package(
        self,
        server: str,
        app: FastAPI,
        http: httpx.AsyncClient,
        live: InMemoryRealtime,
        client_id: str,
    ) -> None:
        drone = await register_drone(http)
        order_id = await place_order(http, client_id)
        async with connect(server, f"/ws/partner/{client_id}/orders/{_external(client_id)}") as ws:
            await frame(ws)
            body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()
            flight_id, drone_id = UUID(body["flight"]["id"]), UUID(drone["id"])
            assigned = await frames_until(ws, lambda f: len(f) >= 1)
            await report(live, sample(drone_id, flight_id, latitude=12.9))
            before_pickup = await quiet(ws)  # assigned, not yet picked up: nothing to show

            await app.state.order_service.transition(
                order_id, S.PICKED_UP, source=EventSource.TELEMETRY
            )
            picked_up = await frames_until(ws, lambda f: len(f) >= 1)
            await app.state.order_service.transition(
                order_id, S.IN_FLIGHT, source=EventSource.TELEMETRY
            )
            await frames_until(ws, lambda f: len(f) >= 1)
            await report(live, sample(drone_id, flight_id, latitude=12.8))
            moving = await frames_until(ws, lambda f: len(f) >= 1)

        assert PARTNER_POSITION[client_id](assigned[0]) is None
        assert before_pickup == []
        # picked up: the last sample is shown at once
        assert PARTNER_STATUS[client_id](picked_up[0]) in {"picked_up", "PICKED_UP"}
        assert PARTNER_LATITUDE[client_id](PARTNER_POSITION[client_id](picked_up[0])) == 12.9
        # in flight: each new sample is a new frame
        assert PARTNER_STATUS[client_id](moving[0]) == IN_FLIGHT_NAME[client_id]
        assert PARTNER_LATITUDE[client_id](PARTNER_POSITION[client_id](moving[0])) == 12.8

    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_nothing_internal_ever_reaches_a_partner(
        self,
        server: str,
        app: FastAPI,
        http: httpx.AsyncClient,
        live: InMemoryRealtime,
        client_id: str,
    ) -> None:
        drone = await register_drone(http)
        order_id = await place_order(http, client_id)
        async with connect(server, f"/ws/partner/{client_id}/orders/{_external(client_id)}") as ws:
            received = [await frame(ws)]
            body = (await http.post(f"/internal/orders/{order_id}/dispatch")).json()
            await app.state.order_service.transition(
                order_id, S.PICKED_UP, source=EventSource.TELEMETRY
            )
            await report(
                live,
                sample(UUID(drone["id"]), UUID(body["flight"]["id"]), battery=13.0),
            )
            received += await quiet(ws, 1.0)

        assert len(received) >= 3  # the snapshot, the status changes, and a position
        text = json.dumps(received).lower()
        for secret in (
            drone["id"],
            body["flight"]["id"],
            "battery",
            "permission",
            "mission",
            "phase",
            "13.0",
        ):
            assert secret.lower() not in text, secret

    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_the_stream_ends_when_the_order_does(
        self, server: str, http: httpx.AsyncClient, client_id: str
    ) -> None:
        await place_order(http, client_id)
        async with connect(server, f"/ws/partner/{client_id}/orders/{_external(client_id)}") as ws:
            await frame(ws)

            await http.post(f"/partner/{client_id}/orders/{_external(client_id)}/cancel")
            final = await frame(ws)
            code = await close_code(ws)

        assert PARTNER_STATUS[client_id](final) in {"cancelled", "CANCELLED"}
        assert code == 1000

    async def test_an_order_that_is_already_finished_sends_its_state_and_closes(
        self, server: str, http: httpx.AsyncClient
    ) -> None:
        await place_order(http, "mock")
        await http.post("/partner/mock/orders/ORD-1/cancel")

        async with connect(server, "/ws/partner/mock/orders/ORD-1") as ws:
            snapshot = await frame(ws)
            code = await close_code(ws)

        assert snapshot["status"] == "cancelled"
        assert code == 1000

    @pytest.mark.parametrize("client_id", CLIENTS)
    async def test_an_unknown_order_is_refused_in_the_partners_error_format(
        self, server: str, client_id: str
    ) -> None:
        async with connect(server, f"/ws/partner/{client_id}/orders/NO-SUCH-ORDER") as ws:
            error = await frame(ws)
            code = await close_code(ws)

        assert PARTNER_NOT_FOUND[client_id](error) == "not_found"
        assert code == 4404

    async def test_an_unknown_client_is_refused(self, server: str) -> None:
        async with connect(server, "/ws/partner/nobody/orders/X") as ws:
            error = await frame(ws)
            code = await close_code(ws)

        assert error["error"]["code"] == "unknown_client"
        assert code == 4404

    async def test_one_partner_cannot_watch_anothers_order(
        self, server: str, http: httpx.AsyncClient
    ) -> None:
        await place_order(http, "mock")

        async with connect(server, "/ws/partner/swiggy/orders/ORD-1") as ws:
            error = await frame(ws)
            code = await close_code(ws)

        assert PARTNER_NOT_FOUND["swiggy"](error) == "not_found"
        assert code == 4404

    async def test_the_partner_stream_ignores_what_the_client_sends(
        self, server: str, app: FastAPI, http: httpx.AsyncClient
    ) -> None:
        order_id, _, _ = await dispatched(http)
        link: HookedLink = app.state.drone_link
        sent = list(link.commands)
        async with connect(server, "/ws/partner/mock/orders/ORD-1") as ws:
            await frame(ws)

            await ws.send(json.dumps({"command": "land"}))
            replies = await quiet(ws)

        assert replies == []
        assert link.commands == sent
        assert (await http.get(f"/internal/orders/{order_id}")).json()["status"] == "assigned"


def _external(client_id: str) -> str:
    return EXTERNAL_IDS[client_id]
