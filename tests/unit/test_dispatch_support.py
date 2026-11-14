"""DispatchService support for telemetry: battery, live flights, the reaper, notifications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog

from app.dispatch.service import DispatchOutcome, StaleRelease
from app.domain.exceptions import DroneValidationError
from app.domain.models import (
    AuditEvent,
    DroneStatus,
    EventSource,
    FlightStatus,
    OrderStatus,
)
from tests.conftest import FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

S = FlightStatus
D = DroneStatus
STALE = timedelta(minutes=5)


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def rig(database: InMemoryDatabase, clock: FakeClock) -> Rig:
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


class TestBattery:
    async def test_a_new_level_is_stored(self, rig: Rig, clock: FakeClock) -> None:
        drone = await rig.add_drone(battery_percent=90.0)
        clock.advance(seconds=30)

        changed = await rig.dispatch.record_battery(drone.id, 64.5)

        stored = await rig.drone(drone.id)
        assert changed is True
        assert stored.battery_percent == 64.5
        assert stored.updated_at == clock()

    async def test_the_same_level_changes_nothing(
        self, rig: Rig, database: InMemoryDatabase
    ) -> None:
        drone = await rig.add_drone(battery_percent=90.0)
        commits = database.commits

        assert await rig.dispatch.record_battery(drone.id, 90.0) is False

        assert database.commits == commits

    async def test_an_unknown_drone_is_ignored(self, rig: Rig) -> None:
        assert await rig.dispatch.record_battery(UUID(int=404), 50.0) is False

    @pytest.mark.parametrize("level", [-1.0, 100.5, float("nan")])
    async def test_an_impossible_level_is_refused_and_nothing_changes(
        self, rig: Rig, level: float
    ) -> None:
        drone = await rig.add_drone(battery_percent=90.0)

        with pytest.raises(DroneValidationError):
            await rig.dispatch.record_battery(drone.id, level)

        assert (await rig.drone(drone.id)).battery_percent == 90.0

    async def test_selection_uses_the_battery_the_drone_has_now(self, rig: Rig) -> None:
        drone = await rig.add_drone(battery_percent=95.0)
        order = await rig.new_order()
        await rig.dispatch.record_battery(drone.id, 10.0)  # it has been flying all day

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is DispatchOutcome.NO_DRONE_AVAILABLE
        assert [r.value for r in result.rejections[drone.id]] == ["battery_low"]


class TestLiveFlights:
    async def test_only_unfinished_flights_are_listed(self, rig: Rig) -> None:
        await rig.add_drone("a")
        await rig.add_drone("b")
        first, second = await rig.new_order(), await rig.new_order()
        live = (await rig.dispatch.dispatch_order(first.id)).flight
        done = (await rig.dispatch.dispatch_order(second.id)).flight
        assert live is not None
        assert done is not None
        await rig.dispatch.return_to_home(done.id)

        listed = await rig.dispatch.list_live_flights()

        assert [f.id for f in listed] == [live.id]

    async def test_nothing_live(self, rig: Rig) -> None:
        assert await rig.dispatch.list_live_flights() == []


async def plant(
    rig: Rig, database: InMemoryDatabase, status: FlightStatus
) -> tuple[UUID, UUID, UUID]:
    """A flight left in ``status`` by a dispatch that then died: ``(order, flight, drone)``.

    The order is back in ``created`` with no drone, as it would be when the process died
    before the order was assigned.
    """
    drone = await rig.add_drone()
    order = await rig.new_order()
    staged = await rig.dispatch.dispatch_order(order.id, start=False)
    assert staged.flight is not None
    flight = database.flights[staged.flight.id]
    database.flights[flight.id] = replace(
        flight,
        status=status,
        permission=None if status is S.PLANNED else flight.permission,
        acknowledged_checksum=(
            flight.acknowledged_checksum if status is S.MISSION_UPLOADED else None
        ),
    )
    database.orders[order.id] = replace(
        database.orders[order.id], status=OrderStatus.CREATED, drone_id=None
    )
    return order.id, flight.id, drone.id


class TestTheReaper:
    async def test_it_frees_a_reservation_a_dispatch_abandoned(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        order_id, flight_id, drone_id = await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=6)

        released = await rig.dispatch.release_stale_reservations(STALE)

        assert released == [StaleRelease(flight_id, drone_id, order_id, S.PERMISSION_GRANTED)]
        assert (await rig.dispatch.get_flight(flight_id)).status is S.CANCELLED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert (await rig.order(order_id)).status is OrderStatus.CREATED

    async def test_the_cancellation_is_audited_as_a_system_action(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        order_id, flight_id, _ = await plant(rig, database, S.PLANNED)
        clock.advance(minutes=6)

        await rig.dispatch.release_stale_reservations(STALE)

        event = [e for e in await rig.trail(order_id) if e.flight_id == flight_id][-1]
        assert event.new_state is S.CANCELLED
        assert event.source is EventSource.SYSTEM
        assert event.metadata["reaper"] is True
        assert event.metadata["reason"].startswith("stale reservation")

    @pytest.mark.parametrize("status", [S.PLANNED, S.PERMISSION_GRANTED, S.MISSION_UPLOADED])
    async def test_each_preflight_state_is_reaped(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock, status: FlightStatus
    ) -> None:
        _, flight_id, drone_id = await plant(rig, database, status)
        clock.advance(minutes=6)

        released = await rig.dispatch.release_stale_reservations(STALE)

        assert [r.flight_id for r in released] == [flight_id]
        assert [r.was for r in released] == [status]
        assert (await rig.drone(drone_id)).status is D.AVAILABLE

    async def test_a_staged_flight_on_an_assigned_order_is_deliberate_and_left_alone(
        self, rig: Rig, clock: FakeClock
    ) -> None:
        await rig.add_drone()
        order = await rig.new_order()
        staged = await rig.dispatch.dispatch_order(order.id, start=False)
        assert staged.flight is not None
        clock.advance(hours=3)

        released = await rig.dispatch.release_stale_reservations(STALE)

        assert released == []
        assert (await rig.dispatch.get_flight(staged.flight.id)).status is S.MISSION_UPLOADED
        assert (await rig.order(order.id)).status is OrderStatus.ASSIGNED

    @pytest.mark.parametrize("pause", [False, True])
    async def test_a_flight_in_the_air_is_never_reaped_however_old(
        self, rig: Rig, clock: FakeClock, pause: bool
    ) -> None:
        await rig.add_drone()
        order = await rig.new_order()
        flying = (await rig.dispatch.dispatch_order(order.id)).flight
        assert flying is not None
        if pause:
            await rig.dispatch.pause_flight(flying.id)
        clock.advance(hours=12)

        assert await rig.dispatch.release_stale_reservations(STALE) == []
        assert not (await rig.dispatch.get_flight(flying.id)).status.is_terminal

    async def test_a_young_reservation_is_left_alone(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        _, flight_id, _ = await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=4)

        assert await rig.dispatch.release_stale_reservations(STALE) == []
        assert (await rig.dispatch.get_flight(flight_id)).status is S.PERMISSION_GRANTED

    async def test_finished_flights_are_not_touched(self, rig: Rig, clock: FakeClock) -> None:
        await rig.add_drone()
        rig.compliance.close_airspace("closed")
        order = await rig.new_order()
        denied = (await rig.dispatch.dispatch_order(order.id)).flight
        assert denied is not None
        clock.advance(hours=1)

        assert await rig.dispatch.release_stale_reservations(STALE) == []
        assert (await rig.dispatch.get_flight(denied.id)).status is S.PERMISSION_DENIED

    async def test_the_order_can_be_dispatched_again_afterwards(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        order_id, _, _ = await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=6)
        await rig.dispatch.release_stale_reservations(STALE)

        result = await rig.dispatch.dispatch_order(order_id)

        assert result.outcome is DispatchOutcome.STARTED

    async def test_reaping_twice_is_harmless(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=6)

        first = await rig.dispatch.release_stale_reservations(STALE)
        second = await rig.dispatch.release_stale_reservations(STALE)

        assert len(first) == 1
        assert second == []

    async def test_a_flight_that_moved_on_while_it_was_being_considered_is_skipped(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        _, flight_id, _ = await plant(rig, database, S.PERMISSION_GRANTED)
        clock.advance(minutes=6)
        original = rig.dispatch._flight_or_none

        async def moved_on(fid: UUID) -> Any:
            flight = await original(fid)
            assert flight is not None
            return replace(  # it progressed since the listing
                flight, status=S.MISSION_UPLOADED, acknowledged_checksum=flight.mission.checksum
            )

        rig.dispatch._flight_or_none = moved_on  # type: ignore[method-assign]

        assert await rig.dispatch.release_stale_reservations(STALE) == []
        rig.dispatch._flight_or_none = original  # type: ignore[method-assign]
        assert (await rig.dispatch.get_flight(flight_id)).status is S.PERMISSION_GRANTED

    async def test_a_dispatch_that_was_still_running_finds_its_flight_cancelled(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        """The reaper may cancel under a dispatch that is merely slow: it must cope."""
        await rig.add_drone()
        order = await rig.new_order()

        async def reaped_mid_upload() -> None:
            clock.advance(minutes=10)
            await rig.dispatch.release_stale_reservations(STALE)

        rig.link.on_upload = reaped_mid_upload

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is DispatchOutcome.CANCELLED_DURING_DISPATCH
        assert (await rig.order(order.id)).status is OrderStatus.CREATED
        assert "start" not in rig.commands()

    async def test_it_logs_each_release(
        self, rig: Rig, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        await plant(rig, database, S.PLANNED)
        clock.advance(minutes=6)

        with structlog.testing.capture_logs() as logs:
            await rig.dispatch.release_stale_reservations(STALE)

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "stale_reservation_released"
        ]


class Recorder:
    """A notifier that remembers what it was told, and what the database held at that moment."""

    def __init__(self, rig_holder: dict[str, Rig] | None = None) -> None:
        self.events: list[AuditEvent] = []
        self.visible_when_told: list[bool] = []
        self.holder = rig_holder if rig_holder is not None else {}

    async def notify(self, events: Sequence[AuditEvent]) -> None:
        rig = self.holder.get("rig")
        for event in events:
            self.events.append(event)
            if rig is not None:
                trail = await rig.trail(event.order_id)
                self.visible_when_told.append(any(e.event_id == event.event_id for e in trail))


class TestNotifications:
    @pytest.fixture
    def recorder(self) -> Recorder:
        return Recorder()

    @pytest.fixture
    def watched(self, database: InMemoryDatabase, clock: FakeClock, recorder: Recorder) -> Rig:
        rig = build_rig(lambda: InMemoryUnitOfWork(database), clock=clock, notifier=recorder)
        recorder.holder["rig"] = rig
        return rig

    async def test_every_committed_change_of_a_dispatch_is_announced_in_order(
        self, watched: Rig, recorder: Recorder
    ) -> None:
        await watched.add_drone()
        order = await watched.new_order()

        await watched.dispatch.dispatch_order(order.id)

        assert [e.new_state.value for e in recorder.events] == [
            "created",
            "planned",
            "permission_granted",
            "mission_uploaded",
            "assigned",
            "started",
        ]

    async def test_what_is_announced_is_exactly_what_the_audit_trail_holds(
        self, watched: Rig, recorder: Recorder
    ) -> None:
        await watched.add_drone()
        order = await watched.new_order()
        await watched.dispatch.dispatch_order(order.id)
        trail = await watched.trail(order.id)

        assert [e.event_id for e in recorder.events] == [e.event_id for e in trail]

    async def test_announcements_follow_the_commit_never_precede_it(
        self, watched: Rig, recorder: Recorder
    ) -> None:
        await watched.add_drone()
        order = await watched.new_order()

        await watched.dispatch.dispatch_order(order.id)

        assert recorder.visible_when_told
        assert all(recorder.visible_when_told)

    async def test_a_rejected_change_is_not_announced(
        self, watched: Rig, recorder: Recorder
    ) -> None:
        order = await watched.new_order()
        told = len(recorder.events)

        with pytest.raises(Exception):  # noqa: B017, PT011 - any domain error
            await watched.orders.transition(order.id, OrderStatus.DELIVERED, source=EventSource.API)

        assert len(recorder.events) == told

    async def test_cancellation_and_progress_are_announced_too(
        self, watched: Rig, recorder: Recorder
    ) -> None:
        await watched.add_drone()
        order = await watched.new_order()
        started = await watched.dispatch.dispatch_order(order.id)
        assert started.flight is not None
        told = len(recorder.events)

        await watched.dispatch.cancel_order(order.id, source=EventSource.API)

        assert [e.new_state.value for e in recorder.events[told:]] == ["aborted", "cancelled"]

    async def test_a_failing_notifier_never_breaks_the_work(
        self, database: InMemoryDatabase, clock: FakeClock
    ) -> None:
        class Exploding:
            async def notify(self, events: Sequence[AuditEvent]) -> None:
                raise RuntimeError("the live view is down")

        rig = build_rig(lambda: InMemoryUnitOfWork(database), clock=clock, notifier=Exploding())
        await rig.add_drone()
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is DispatchOutcome.STARTED
        assert (await rig.order(order.id)).status is OrderStatus.ASSIGNED
        assert len(await rig.trail(order.id)) == 6  # everything was still recorded

    async def test_no_notifier_is_fine(self, rig: Rig) -> None:
        await rig.add_drone()
        order = await rig.new_order()

        assert (await rig.dispatch.dispatch_order(order.id)).succeeded
