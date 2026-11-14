"""The reaper sweep, the periodic runner, and the ingest's fault alert."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog

from app.dispatch.service import StaleRelease
from app.domain.models import FlightStatus, OrderStatus
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Alert, AlertKind
from app.telemetry.ingest import IngestOutcome, TelemetryIngest
from app.telemetry.periodic import every
from app.telemetry.reaper import Reaper
from tests.conftest import T0, FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork
from tests.unit.test_dispatch_support import plant

S = FlightStatus


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def rig(database: InMemoryDatabase, clock: FakeClock) -> Rig:
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


@pytest.fixture
def live(clock: FakeClock) -> InMemoryRealtime:
    return InMemoryRealtime(clock=clock)


class TestTheReaperSweep:
    async def test_a_release_is_announced_as_a_one_off_alert(
        self,
        rig: Rig,
        database: InMemoryDatabase,
        live: InMemoryRealtime,
        clock: FakeClock,
    ) -> None:
        order_id, flight_id, drone_id = await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=6)
        reaper = Reaper(rig.dispatch, live, older_than=timedelta(minutes=5), clock=clock)

        released = await reaper.sweep()

        assert released == [StaleRelease(flight_id, drone_id, order_id, S.PERMISSION_GRANTED)]
        (alert,) = await live.recent()
        assert alert.kind is AlertKind.STALE_RESERVATION
        assert (alert.drone_id, alert.flight_id, alert.order_id) == (drone_id, flight_id, order_id)
        assert "permission_granted" in alert.message
        assert await live.active() == []  # one-off: there is nothing to clear
        assert (await rig.order(order_id)).status is OrderStatus.CREATED

    async def test_nothing_stale_nothing_announced(
        self, rig: Rig, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        reaper = Reaper(rig.dispatch, live, clock=clock)

        assert await reaper.sweep() == []
        assert await live.recent() == []

    async def test_a_failing_alert_board_does_not_undo_or_hide_the_release(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        _, flight_id, drone_id = await plant(rig, database, S.PLANNED)
        clock.advance(minutes=6)
        broken = InMemoryRealtime(clock=clock)
        broken.broken = True
        reaper = Reaper(rig.dispatch, broken, clock=clock)

        released = await reaper.sweep()

        assert [r.flight_id for r in released] == [flight_id]
        assert (await rig.dispatch.get_flight(flight_id)).status is S.CANCELLED
        assert (await rig.drone(drone_id)).status.value == "available"


class TestPeriodic:
    async def test_it_runs_the_job_repeatedly_until_cancelled(self) -> None:
        runs = 0

        async def job() -> None:
            nonlocal runs
            runs += 1

        task = asyncio.create_task(every(0.01, job, name="count"))
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert runs >= 4

    async def test_a_failing_run_is_logged_and_the_loop_carries_on(self) -> None:
        runs = 0

        async def job() -> None:
            nonlocal runs
            runs += 1
            if runs == 2:
                raise ConnectionError("redis hiccup")

        with structlog.testing.capture_logs() as logs:
            task = asyncio.create_task(every(0.01, job, name="flaky"))
            await asyncio.sleep(0.12)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert runs > 2  # it survived the failure
        assert [e["job"] for e in logs if e["event"] == "periodic_job_failed"] == ["flaky"]


def fault(flight_id: UUID, seq: int = 1, **detail: Any) -> bytes:
    return json.dumps(
        {
            "event_id": str(UUID(int=0xE000 + seq)),
            "flight_id": str(flight_id),
            "seq": seq,
            "type": "fault",
            "occurred_at": T0.isoformat(),
            "detail": detail,
        }
    ).encode()


class TestFaultAlerts:
    async def started(self, rig: Rig) -> tuple[UUID, UUID]:
        drone = await rig.add_drone()
        order = await rig.new_order()
        result = await rig.dispatch.dispatch_order(order.id)
        assert result.flight is not None
        return drone.id, result.flight.id

    async def test_a_fault_becomes_a_one_off_alert_and_nothing_else_happens(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        drone_id, flight_id = await self.started(rig)
        sent = list(rig.link.commands)
        ingest = TelemetryIngest(rig.dispatch, alerts=live)

        result = await ingest.handle_event(
            drone_id, fault(flight_id, code="GPS_LOSS", message="no fix for 20 s")
        )

        assert result.outcome is IngestOutcome.NOTED
        (alert,) = await live.recent()
        assert alert.kind is AlertKind.FAULT
        assert alert.message == "GPS_LOSS: no fix for 20 s"
        assert (alert.drone_id, alert.flight_id) == (drone_id, flight_id)
        assert await live.active() == []
        assert rig.link.commands == sent  # alerts only: no recall, no landing

    async def test_the_drones_text_is_bounded(self, rig: Rig, live: InMemoryRealtime) -> None:
        drone_id, flight_id = await self.started(rig)

        await TelemetryIngest(rig.dispatch, alerts=live).handle_event(
            drone_id, fault(flight_id, code="C" * 500, message="M" * 5000)
        )

        (alert,) = await live.recent()
        assert len(alert.message) <= 60 + 2 + 200

    async def test_a_fault_without_details_is_still_an_alert(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        drone_id, flight_id = await self.started(rig)

        await TelemetryIngest(rig.dispatch, alerts=live).handle_event(drone_id, fault(flight_id))

        (alert,) = await live.recent()
        assert alert.message == "unknown"

    async def test_progress_events_raise_no_alert(self, rig: Rig, live: InMemoryRealtime) -> None:
        drone_id, flight_id = await self.started(rig)
        payload = json.dumps(
            {
                "event_id": str(UUID(int=1)),
                "flight_id": str(flight_id),
                "seq": 1,
                "type": "package_picked_up",
                "occurred_at": T0.isoformat(),
            }
        ).encode()

        await TelemetryIngest(rig.dispatch, alerts=live).handle_event(drone_id, payload)

        assert await live.recent() == []

    async def test_a_failing_alert_board_does_not_change_the_result(self, rig: Rig) -> None:
        drone_id, flight_id = await self.started(rig)
        broken = InMemoryRealtime()
        broken.broken = True

        result = await TelemetryIngest(rig.dispatch, alerts=broken).handle_event(
            drone_id, fault(flight_id, code="X")
        )

        assert result.outcome is IngestOutcome.NOTED

    async def test_no_alert_board_is_fine(self, rig: Rig) -> None:
        drone_id, flight_id = await self.started(rig)

        result = await TelemetryIngest(rig.dispatch).handle_event(drone_id, fault(flight_id))

        assert result.outcome is IngestOutcome.NOTED

    def test_the_alert_is_a_model_the_board_understands(self) -> None:
        assert (
            Alert(kind=AlertKind.FAULT, drone_id=None, message="x", raised_at=T0).key == "fault:-"
        )
