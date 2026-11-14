"""The whole platform over a real MQTT broker: API -> Mosquitto -> simulated drone -> worker.

Real everything: the FastAPI app (lifespan, PostgreSQL), a real Mosquitto, the telemetry
worker, and simulated drones. Nothing is faked on the wire, so this is where the command/ack
protocol, the durable session and the event handling are proven to work together.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.mqtt.client import MqttClient
from app.protocol import messages as wire
from app.protocol import topics
from app.services.ports import UnitOfWorkFactory
from app.simulator.drone import Phase
from tests.api.conftest import place_order
from tests.conftest import T0
from tests.integration.conftest import RunSql
from tests.integration.stack import (
    Stack,
    audit,
    counted,
    eventually,
    have,
    is_,
    order_is,
    picked_up_event,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def stack(
    database_url: str, mosquitto_url: str, pg_uow_factory: UnitOfWorkFactory
) -> AsyncIterator[Stack]:
    api_client = MqttClient(mosquitto_url, client_id=f"api-{uuid4().hex[:10]}")
    app = create_app(
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=database_url,
            drone_link="mqtt",
            mqtt_url=mosquitto_url,
            mqtt_ack_timeout_seconds=1.5,
        ),
        mqtt_client=api_client,
    )
    async with app.router.lifespan_context(app):
        await api_client.wait_connected(5)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://platform.test") as http:
            built = Stack(http, app, mosquitto_url, pg_uow_factory)
            await built.start_worker()
            try:
                yield built
            finally:
                await built.close()


class TestADeliveryOverRealMqtt:
    async def test_dispatch_to_delivered_with_no_human_in_the_loop(
        self, stack: Stack, run_sql: RunSql
    ) -> None:
        drone = await stack.add_drone()
        order_id = await place_order(stack.http)

        dispatched = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()
        assert dispatched["outcome"] == "started"

        async def delivered() -> bool:
            return (await stack.order(order_id))["status"] == "delivered"

        await eventually(delivered)

        async def completed() -> bool:
            return [f["status"] for f in await stack.flights(order_id)] == ["completed"]

        await eventually(completed)
        await eventually(lambda: is_(stack.drone_status(drone.drone_id), "available"))
        assert drone.phase is Phase.IDLE

        trail = await audit(run_sql, order_id)
        assert [(kind, state) for kind, state, _ in trail] == [
            ("order_created", "created"),
            ("flight_created", "planned"),
            ("flight_status_changed", "permission_granted"),
            ("flight_status_changed", "mission_uploaded"),
            ("order_status_changed", "assigned"),
            ("flight_status_changed", "started"),
            ("order_status_changed", "picked_up"),
            ("order_status_changed", "in_flight"),
            ("order_status_changed", "arriving"),
            ("order_status_changed", "delivered"),
            ("flight_status_changed", "completed"),
        ]
        telemetry = [state for _, state, source in trail if source == "telemetry"]
        assert telemetry == ["picked_up", "in_flight", "arriving", "delivered", "completed"]

    async def test_the_same_drone_then_takes_the_next_order(self, stack: Stack) -> None:
        drone = await stack.add_drone()
        first = await place_order(stack.http)
        await stack.http.post(f"/internal/orders/{first}/dispatch")
        await eventually(lambda: is_(stack.drone_status(drone.drone_id), "available"))
        await eventually(lambda: order_is(stack, first, "delivered"))

        second_response = await stack.http.post(
            "/partner/mock/orders",
            json={
                "order_id": "ORD-2",
                "pickup": {"lat": 12.9716, "lng": 77.5946},
                "dropoff": {"lat": 12.9352, "lng": 77.6245},
                "package": {"weight_kg": 1.0, "description": "tea"},
            },
        )
        second = UUID(second_response.json()["tracking_id"])
        dispatched = (await stack.http.post(f"/internal/orders/{second}/dispatch")).json()

        assert dispatched["outcome"] == "started"
        await eventually(lambda: order_is(stack, second, "delivered"))

    async def test_pause_and_resume_work_over_the_wire(self, stack: Stack) -> None:
        drone = await stack.add_drone(speed_factor=1.0)  # slow: it is still in the air
        order_id = await place_order(stack.http)
        flight_id = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()[
            "flight"
        ]["id"]

        paused = await stack.http.post(f"/internal/flights/{flight_id}/pause")
        assert paused.json()["status"] == "paused"
        assert drone.phase is Phase.PAUSED

        resumed = await stack.http.post(f"/internal/flights/{flight_id}/resume")
        assert resumed.json()["status"] == "started"
        assert drone.phase is Phase.FLYING


class TestFailuresOverRealMqtt:
    async def test_cancelling_a_flying_order_recalls_the_drone_which_docks_itself(
        self, stack: Stack
    ) -> None:
        drone = await stack.add_drone(speed_factor=300.0)  # a ~4 s sortie: still flying
        order_id = await place_order(stack.http)
        await stack.http.post(f"/internal/orders/{order_id}/dispatch")

        cancelled = await stack.http.post("/partner/mock/orders/ORD-1/cancel")

        assert cancelled.status_code == 200
        assert (await stack.order(order_id))["status"] == "cancelled"
        assert [f["status"] for f in await stack.flights(order_id)] == ["aborted"]
        await eventually(lambda: is_(stack.drone_status(drone.drone_id), "available"))
        assert drone.phase is Phase.IDLE
        assert (await stack.order(order_id))["status"] == "cancelled"  # nothing resurrected it

    async def test_a_start_whose_acknowledgement_is_lost_is_recovered(self, stack: Stack) -> None:
        drone = await stack.add_drone(speed_factor=1.0)
        drone.silent.add("start_mission")  # it starts flying, but never says so
        order_id = await place_order(stack.http)

        body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "start_failed"
        assert body["order"]["status"] == "failed"
        assert body["flight"]["status"] == "aborted"
        # the platform could not know it had started, so it recalled it (which the drone obeyed)
        await eventually(lambda: is_(stack.drone_status(drone.drone_id), "available"))
        assert drone.phase is Phase.IDLE

    async def test_a_drone_that_refuses_the_mission_fails_the_upload(self, stack: Stack) -> None:
        drone = await stack.add_drone()
        drone.refuse["upload_mission"] = "storage full"
        order_id = await place_order(stack.http)

        body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "upload_failed"
        assert "storage full" in body["detail"]
        assert body["order"]["status"] == "created"
        assert body["drone"]["status"] == "available"

    async def test_a_drone_nobody_is_playing_never_answers(self, stack: Stack) -> None:
        drone = await stack.add_drone(play=False)
        order_id = await place_order(stack.http)

        body = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "upload_failed"
        assert "no acknowledgement" in body["detail"]
        assert (await stack.drone_status(drone.drone_id)) == "available"
        assert body["order"]["status"] == "created"


class TestDuplicatesAndOutOfOrderEvents:
    async def test_repeated_events_change_the_order_once(
        self, stack: Stack, run_sql: RunSql
    ) -> None:
        drone = await stack.add_drone(speed_factor=1e-6, play=True)  # it makes no events itself
        order_id = await place_order(stack.http)
        flight_id = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()[
            "flight"
        ]["id"]
        topic = topics.event_topic(drone.drone_id)

        for _ in range(3):  # MQTT may deliver the same message more than once
            await stack.publish_raw(topic, picked_up_event(flight_id, 1))
        await stack.publish_raw(topic, picked_up_event(flight_id, 2, "departed_pickup"))
        await stack.publish_raw(topic, picked_up_event(flight_id, 2, "departed_pickup"))

        await eventually(lambda: order_is(stack, order_id, "in_flight"))
        await asyncio.sleep(0.3)  # let any stragglers arrive
        trail = await audit(run_sql, order_id)
        telemetry = [state for _, state, source in trail if source == "telemetry"]
        assert telemetry == ["picked_up", "in_flight"]  # each exactly once
        assert stack.worker is not None
        assert stack.worker.ingest.counts["duplicate"] == 3

    async def test_a_skipped_step_is_refused_not_invented(
        self, stack: Stack, run_sql: RunSql
    ) -> None:
        drone = await stack.add_drone(speed_factor=1e-6)
        order_id = await place_order(stack.http)
        flight_id = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()[
            "flight"
        ]["id"]

        await stack.publish_raw(
            topics.event_topic(drone.drone_id),
            picked_up_event(flight_id, 1, "package_delivered"),  # nothing was picked up
        )

        assert stack.worker is not None
        await eventually(lambda: counted(stack, "rejected", 1))
        assert (await stack.order(order_id))["status"] == "assigned"
        assert [s for _, s, src in await audit(run_sql, order_id) if src == "telemetry"] == []

    async def test_garbage_on_the_events_topic_does_not_hurt_the_worker(self, stack: Stack) -> None:
        drone = await stack.add_drone(speed_factor=1e-6)
        topic = topics.event_topic(drone.drone_id)

        await stack.publish_raw(topic, b"not json at all")
        await stack.publish_raw(topic, b'{"type": "teleported"}')

        await eventually(lambda: counted(stack, "invalid", 2))
        # and the worker is still alive and useful
        order_id = await place_order(stack.http)
        flight_id = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()[
            "flight"
        ]["id"]
        await stack.publish_raw(topic, picked_up_event(flight_id, 1))
        await eventually(lambda: order_is(stack, order_id, "picked_up"))


class TestTheDurableSession:
    async def test_events_sent_while_the_worker_is_down_are_processed_when_it_returns(
        self, stack: Stack
    ) -> None:
        drone = await stack.add_drone(speed_factor=1e-6)
        order_id = await place_order(stack.http)
        flight_id = (await stack.http.post(f"/internal/orders/{order_id}/dispatch")).json()[
            "flight"
        ]["id"]
        await stack.stop_worker()  # the worker is away (a deploy, a crash)...

        topic = topics.event_topic(drone.drone_id)
        await stack.publish_raw(topic, picked_up_event(flight_id, 1))
        await stack.publish_raw(topic, picked_up_event(flight_id, 2, "departed_pickup"))
        assert (await stack.order(order_id))["status"] == "assigned"  # nobody has seen them yet

        await stack.start_worker()  # ...and comes back with the same identity

        await eventually(lambda: order_is(stack, order_id, "in_flight"))


class TestRule2OnTheWire:
    async def test_a_drone_refuses_an_expired_authorisation_sent_over_the_broker(
        self, stack: Stack
    ) -> None:
        """The drone does not trust the cloud: it checks the authorisation itself."""
        drone = await stack.add_drone(speed_factor=1.0)
        order_id = await place_order(stack.http)
        dispatched = (
            await stack.http.post(f"/internal/orders/{order_id}/dispatch", params={"start": False})
        ).json()
        flight = dispatched["flight"]
        assert drone.phase is Phase.READY  # the mission is on board, not started
        acks: list[wire.AckMessage] = []

        async def collect(topic: str, payload: bytes) -> None:
            acks.append(wire.decode_ack(payload))

        listener = MqttClient(stack.mqtt_url, client_id=f"listener-{uuid4().hex[:8]}")
        task = asyncio.create_task(listener.run({"platform/acks/forged": collect}))
        await listener.wait_connected(5)
        expired = wire.StartMission(
            command_id=uuid4(),
            drone_id=drone.drone_id,
            sent_at=T0,
            reply_to="platform/acks/forged",
            authorization=wire.WireAuthorization(
                flight_id=UUID(flight["id"]),
                drone_id=drone.drone_id,
                permission_id="FORGED",
                mission_checksum=flight["mission"]["checksum"],
                authorized_at=T0,
                valid_until=T0 + timedelta(minutes=1),  # long past
            ),
        )

        await stack.publish_raw(topics.command_topic(drone.drone_id), wire.encode(expired))
        await eventually(lambda: have(acks))
        task.cancel()

        assert acks[0].status is wire.AckStatus.REJECTED
        assert acks[0].reason is not None
        assert "expired" in acks[0].reason
        assert drone.phase is Phase.READY  # it did not start


class TestReadiness:
    async def test_the_app_is_ready_once_it_is_connected_to_the_broker(self, stack: Stack) -> None:
        assert (await stack.http.get("/ready")).status_code == 200
