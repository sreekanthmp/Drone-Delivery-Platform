"""The telemetry recorder: live state, broadcast, and an honest stored battery."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from app.domain.models import DroneStatus
from app.realtime import channels
from app.realtime.memory import InMemoryRealtime
from app.telemetry.recorder import SampleOutcome, TelemetryRecorder
from tests.conftest import FLIGHT_ID, T0, FakeClock, build_drone
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

R = SampleOutcome


def sample(**overrides: Any) -> bytes:
    fields: dict[str, Any] = {
        "sent_at": T0.isoformat(),
        "latitude": 12.97,
        "longitude": 77.59,
        "altitude_m": 60.0,
        "battery_percent": 88.0,
        "phase": "flying",
        "flight_id": str(FLIGHT_ID),
    }
    fields.update(overrides)
    return json.dumps(fields).encode()


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def rig(database: InMemoryDatabase, clock: FakeClock) -> Rig:
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


@pytest.fixture
def live(clock: FakeClock) -> InMemoryRealtime:
    return InMemoryRealtime(clock=clock)


@pytest.fixture
def monotonic() -> Monotonic:
    return Monotonic()


@pytest.fixture
def recorder(
    rig: Rig, live: InMemoryRealtime, clock: FakeClock, monotonic: Monotonic
) -> TelemetryRecorder:
    return TelemetryRecorder(rig.dispatch, live, live, clock=clock, monotonic=monotonic)


class TestRecording:
    async def test_the_latest_state_is_kept_live_with_the_platforms_receive_time(
        self, rig: Rig, recorder: TelemetryRecorder, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        drone = await rig.add_drone()
        clock.advance(seconds=3)

        outcome = await recorder.handle_sample(drone.id, sample())

        assert outcome is R.RECORDED
        state = await live.get_state(drone.id)
        assert state is not None
        assert (state.latitude, state.longitude, state.altitude_m) == (12.97, 77.59, 60.0)
        assert state.battery_percent == 88.0
        assert state.phase.value == "flying"
        assert state.flight_id == FLIGHT_ID
        assert state.sent_at == T0  # the drone's clock
        assert state.received_at == clock()  # the platform's clock

    async def test_it_is_broadcast_to_the_drone_and_the_fleet_as_droppable(
        self, rig: Rig, recorder: TelemetryRecorder, live: InMemoryRealtime
    ) -> None:
        drone = await rig.add_drone()

        await recorder.handle_sample(drone.id, sample())

        assert [c for c, _ in live.published] == [channels.drone_channel(drone.id), channels.FLEET]
        envelope = live.published[0][1]
        assert envelope.kind == "drone_state"
        assert envelope.droppable is True
        assert envelope.data["drone_id"] == str(drone.id)
        assert envelope.data["battery_percent"] == 88.0

    async def test_garbage_is_invalid_and_never_raises(
        self, rig: Rig, recorder: TelemetryRecorder, live: InMemoryRealtime
    ) -> None:
        drone = await rig.add_drone()

        for payload in (b"", b"nope", b"{}", sample(latitude=200)):
            assert await recorder.handle_sample(drone.id, payload) is R.INVALID

        assert live.published == []
        assert await live.all_states() == {}

    async def test_a_sample_changes_no_status_and_sends_no_command(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        started = await rig.dispatch.dispatch_order(order.id)
        assert started.flight is not None
        sent = list(rig.link.commands)
        trail = len(await rig.trail(order.id))

        await recorder.handle_sample(drone.id, sample(phase="idle", flight_id=None))

        assert (await rig.drone(drone.id)).status is DroneStatus.ON_MISSION
        assert (await rig.dispatch.get_flight(started.flight.id)).status.value == "started"
        assert rig.link.commands == sent
        assert len(await rig.trail(order.id)) == trail

    async def test_outcomes_are_counted(self, rig: Rig, recorder: TelemetryRecorder) -> None:
        drone = await rig.add_drone()

        await recorder.handle_sample(drone.id, sample())
        await recorder.handle_sample(drone.id, b"junk")
        await recorder.handle_sample(UUID(int=404), sample())

        assert dict(recorder.counts) == {R.RECORDED: 1, R.INVALID: 1, R.UNKNOWN_DRONE: 1}


class TestStrangers:
    async def test_a_sample_from_an_unregistered_drone_is_ignored(
        self, recorder: TelemetryRecorder, live: InMemoryRealtime
    ) -> None:
        outcome = await recorder.handle_sample(UUID(int=404), sample())

        assert outcome is R.UNKNOWN_DRONE
        assert await live.all_states() == {}
        assert live.published == []

    async def test_a_stranger_is_not_looked_up_again_for_a_while(
        self, rig: Rig, recorder: TelemetryRecorder, monotonic: Monotonic
    ) -> None:
        lookups = 0
        original = rig.dispatch.drone_exists

        async def counting(drone_id: UUID) -> bool:
            nonlocal lookups
            lookups += 1
            return await original(drone_id)

        rig.dispatch.drone_exists = counting  # type: ignore[method-assign]
        for _ in range(50):
            await recorder.handle_sample(UUID(int=404), sample())
        assert lookups == 1

        monotonic.now += 61  # the cache entry lapses
        await recorder.handle_sample(UUID(int=404), sample())
        assert lookups == 2

    async def test_a_drone_registered_after_it_was_refused_is_accepted_once_the_cache_lapses(
        self,
        recorder: TelemetryRecorder,
        database: InMemoryDatabase,
        monotonic: Monotonic,
    ) -> None:
        newcomer = UUID(int=0x777)
        assert await recorder.handle_sample(newcomer, sample()) is R.UNKNOWN_DRONE

        database.drones[newcomer] = build_drone(id=newcomer, name="newcomer")  # registered since
        assert await recorder.handle_sample(newcomer, sample()) is R.UNKNOWN_DRONE  # still cached

        monotonic.now += 61
        assert await recorder.handle_sample(newcomer, sample()) is R.RECORDED

    async def test_a_registered_drone_is_looked_up_only_once(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone()
        lookups = 0
        original = rig.dispatch.drone_exists

        async def counting(drone_id: UUID) -> bool:
            nonlocal lookups
            lookups += 1
            return await original(drone_id)

        rig.dispatch.drone_exists = counting  # type: ignore[method-assign]

        for _ in range(20):
            await recorder.handle_sample(drone.id, sample())

        assert lookups == 1

    async def test_a_flood_of_strangers_cannot_grow_memory_without_bound(
        self, recorder: TelemetryRecorder
    ) -> None:
        for n in range(1, 1600):
            await recorder.handle_sample(UUID(int=0xBAD000 + n), sample())

        assert len(recorder._unknown_until) <= 1000


class TestBatterySync:
    async def test_the_first_sample_brings_the_stored_battery_in_line(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)

        await recorder.handle_sample(drone.id, sample(battery_percent=87.34))

        assert (await rig.drone(drone.id)).battery_percent == 87.3

    async def test_small_changes_are_not_written(
        self, rig: Rig, recorder: TelemetryRecorder, database: InMemoryDatabase
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        await recorder.handle_sample(drone.id, sample(battery_percent=90.0))
        commits = database.commits

        for level in (89.0, 88.0, 87.0, 86.0, 85.5):  # all within 5 points of 90
            await recorder.handle_sample(drone.id, sample(battery_percent=level))

        assert database.commits == commits
        assert (await rig.drone(drone.id)).battery_percent == 90.0

    async def test_a_step_of_five_points_is_written(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        await recorder.handle_sample(drone.id, sample(battery_percent=90.0))

        await recorder.handle_sample(drone.id, sample(battery_percent=85.0))

        assert (await rig.drone(drone.id)).battery_percent == 85.0

    async def test_recharging_is_followed_too(self, rig: Rig, recorder: TelemetryRecorder) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        await recorder.handle_sample(drone.id, sample(battery_percent=20.0))

        await recorder.handle_sample(drone.id, sample(battery_percent=100.0))

        assert (await rig.drone(drone.id)).battery_percent == 100.0

    async def test_the_battery_is_synced_even_when_the_live_view_is_down(
        self, rig: Rig, recorder: TelemetryRecorder, live: InMemoryRealtime
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        live.broken = True

        outcome = await recorder.handle_sample(drone.id, sample(battery_percent=40.0))

        assert outcome is R.LIVE_VIEW_DOWN
        assert (await rig.drone(drone.id)).battery_percent == 40.0

    async def test_a_failed_write_is_retried_on_the_next_sample(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        original = rig.dispatch.record_battery
        calls = {"n": 0}

        async def flaky(drone_id: UUID, level: float) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("database restarting")
            return await original(drone_id, level)

        rig.dispatch.record_battery = flaky  # type: ignore[method-assign]

        first = await recorder.handle_sample(drone.id, sample(battery_percent=60.0))
        second = await recorder.handle_sample(drone.id, sample(battery_percent=60.0))

        assert (first, second) == (R.RECORDED, R.RECORDED)
        assert (await rig.drone(drone.id)).battery_percent == 60.0  # not forgotten
        assert calls["n"] == 2

    async def test_selection_then_sees_the_real_battery(
        self, rig: Rig, recorder: TelemetryRecorder
    ) -> None:
        drone = await rig.add_drone(battery_percent=100.0)
        order = await rig.new_order()
        await recorder.handle_sample(
            drone.id, sample(battery_percent=12.0, phase="idle", flight_id=None)
        )

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome.value == "no_drone_available"
