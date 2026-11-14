"""The telemetry worker's powerlessness, and its wiring."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from app.compliance.disabled import DisabledCompliance
from app.compliance.interface import ComplianceUnavailableError
from app.dispatch.link import CommandRejectedError, DisabledDroneLink
from app.dispatch.progress import ProgressEvent
from app.dispatch.service import DispatchOutcome, DispatchService
from app.domain.models import EventSource, OrderStatus
from app.protocol import topics
from app.realtime.memory import InMemoryRealtime
from app.services.order_service import OrderService
from app.telemetry.ingest import IngestOutcome
from app.worker import Worker
from tests.conftest import DRONE_ID, T0, FakeClock, build_mission, build_permission
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork


class TestDisabledLink:
    @pytest.mark.parametrize("operation", ["pause_mission", "return_to_home", "land"])
    async def test_simple_commands_are_refused(self, operation: str) -> None:
        with pytest.raises(CommandRejectedError, match="cannot send commands"):
            await getattr(DisabledDroneLink(), operation)(DRONE_ID)

    async def test_uploads_and_starts_are_refused(self) -> None:
        link = DisabledDroneLink()

        with pytest.raises(CommandRejectedError):
            await link.upload_mission(DRONE_ID, build_mission())
        with pytest.raises(CommandRejectedError):
            await link.start_mission(DRONE_ID, object())  # type: ignore[arg-type]


class TestDisabledCompliance:
    async def test_it_fails_closed(self) -> None:
        provider = DisabledCompliance()

        with pytest.raises(ComplianceUnavailableError):
            await provider.validate_permission(build_permission().permission_id)
        with pytest.raises(ComplianceUnavailableError):
            await provider.request_permission(object())  # type: ignore[arg-type]


class TestAServiceBuiltFromThemCannotFly:
    @pytest.fixture
    def powerless(self, clock: FakeClock) -> tuple[DispatchService, Rig]:
        """A rig for set-up plus a dispatch service wired the way the worker wires it."""
        database = InMemoryDatabase()
        rig = build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)
        service = DispatchService(
            lambda: InMemoryUnitOfWork(database),
            orders=OrderService(lambda: InMemoryUnitOfWork(database)),
            compliance=DisabledCompliance(),
            link=DisabledDroneLink(),
            clock=clock,
        )
        return service, rig

    async def test_it_cannot_dispatch_anything(
        self, powerless: tuple[DispatchService, Rig]
    ) -> None:
        service, rig = powerless
        await rig.add_drone()
        order = await rig.new_order()

        result = await service.dispatch_order(order.id)

        assert result.outcome is DispatchOutcome.PERMISSION_DENIED  # failed closed
        assert result.order.status is OrderStatus.CREATED
        assert rig.link.commands == []

    async def test_but_it_can_still_record_what_a_drone_reports(
        self, powerless: tuple[DispatchService, Rig]
    ) -> None:
        service, rig = powerless
        await rig.add_drone()
        order = await rig.new_order()
        started = await rig.dispatch.dispatch_order(order.id)
        assert started.flight is not None
        assert started.drone is not None

        result = await service.record_progress(
            started.drone.id, started.flight.id, ProgressEvent.PACKAGE_PICKED_UP
        )

        assert result.applied
        assert (await rig.order(order.id)).status is OrderStatus.PICKED_UP


class FakeClient:
    def __init__(self) -> None:
        self.subscriptions: dict[str, Any] = {}

    async def run(self, subscriptions: dict[str, Any]) -> None:
        self.subscriptions = dict(subscriptions)
        await asyncio.Event().wait()


class TestWorker:
    @pytest.fixture
    def rig(self, clock: FakeClock) -> Rig:
        database = InMemoryDatabase()
        return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)

    @pytest.fixture
    def worker(self, rig: Rig) -> Worker:
        return Worker(rig.uow_factory, FakeClient())  # type: ignore[arg-type]

    async def test_with_a_live_view_it_also_records_every_drones_telemetry(self, rig: Rig) -> None:
        client = FakeClient()
        worker = Worker(rig.uow_factory, client, live=InMemoryRealtime())  # type: ignore[arg-type]
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.01)

        assert set(client.subscriptions) == {topics.EVENTS_FILTER, topics.TELEMETRY_FILTER}
        assert client.subscriptions[topics.TELEMETRY_FILTER] == worker.on_sample
        assert worker.recorder is not None
        assert worker.watchdog is not None
        task.cancel()

    async def test_without_a_live_view_it_has_no_recorder_or_watchdog_but_still_a_reaper(
        self, rig: Rig, worker: Worker
    ) -> None:
        assert worker.recorder is None
        assert worker.watchdog is None
        assert worker.reaper is not None

    async def test_a_sample_is_recorded_into_the_live_view(self, rig: Rig) -> None:
        live = InMemoryRealtime()
        worker = Worker(rig.uow_factory, FakeClient(), live=live)  # type: ignore[arg-type]
        drone = await rig.add_drone()
        sample = (
            b'{"sent_at": "%s", "latitude": 12.9, "longitude": 77.5, "altitude_m": 10,'
            b' "battery_percent": 70, "phase": "idle"}'
        ) % T0.isoformat().encode()

        await worker.on_sample(topics.telemetry_topic(drone.id), sample)

        state = await live.get_state(drone.id)
        assert state is not None
        assert state.battery_percent == 70.0

    @pytest.mark.parametrize("topic", ["drones/not-a-uuid/telemetry", "drones/+/telemetry", ""])
    async def test_a_sample_on_a_malformed_topic_is_dropped(self, rig: Rig, topic: str) -> None:
        live = InMemoryRealtime()
        worker = Worker(rig.uow_factory, FakeClient(), live=live)  # type: ignore[arg-type]

        await worker.on_sample(topic, b"{}")

        assert live.published == []

    async def test_a_sample_without_a_live_view_is_ignored(self, rig: Rig, worker: Worker) -> None:
        drone = await rig.add_drone()

        await worker.on_sample(topics.telemetry_topic(drone.id), b"{}")  # no recorder: no crash

    async def test_it_subscribes_to_every_drones_events(self, rig: Rig) -> None:
        client = FakeClient()
        worker = Worker(rig.uow_factory, client)  # type: ignore[arg-type]
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.01)

        assert set(client.subscriptions) == {topics.EVENTS_FILTER}
        assert client.subscriptions[topics.EVENTS_FILTER] == worker.on_event
        task.cancel()

    async def test_an_event_on_a_valid_topic_reaches_the_ingest(
        self, rig: Rig, worker: Worker
    ) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        flight = (await rig.dispatch.dispatch_order(order.id)).flight
        assert flight is not None
        payload = (
            b'{"event_id": "%s", "flight_id": "%s", "seq": 1, "type": "package_picked_up",'
            b' "occurred_at": "%s"}'
        ) % (str(UUID(int=1)).encode(), str(flight.id).encode(), T0.isoformat().encode())

        await worker.on_event(topics.event_topic(drone.id), payload)

        assert (await rig.order(order.id)).status is OrderStatus.PICKED_UP
        assert worker.ingest.counts[IngestOutcome.APPLIED] == 1
        events = [e for e in await rig.trail(order.id) if e.source is EventSource.TELEMETRY]
        assert len(events) == 1

    @pytest.mark.parametrize(
        "topic", ["drones/not-a-uuid/events", "drones/+/events", "other/topic", ""]
    )
    async def test_an_event_on_a_malformed_topic_is_dropped(
        self, worker: Worker, topic: str
    ) -> None:
        await worker.on_event(topic, b"{}")

        assert sum(worker.ingest.counts.values()) == 0

    async def test_its_ingest_cannot_command_a_drone(self, worker: Worker) -> None:
        assert isinstance(worker.ingest._dispatch._link, DisabledDroneLink)
        assert isinstance(worker.ingest._dispatch._compliance, DisabledCompliance)
