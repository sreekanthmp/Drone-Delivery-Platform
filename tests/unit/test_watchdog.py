"""The watchdog: what it notices, when, how it clears, and that it only ever alerts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.models import DroneStatus, FlightStatus
from app.protocol.messages import DronePhase
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import AlertKind, DroneState
from app.telemetry.watchdog import Watchdog, WatchdogConfig
from tests.conftest import FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

K = AlertKind
LOST = timedelta(seconds=15)
GRACE = timedelta(seconds=30)


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def rig(database: InMemoryDatabase, clock: FakeClock) -> Rig:
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


@pytest.fixture
def live(clock: FakeClock) -> InMemoryRealtime:
    return InMemoryRealtime(clock=clock, state_ttl=timedelta(seconds=60))


@pytest.fixture
def watchdog(rig: Rig, live: InMemoryRealtime, clock: FakeClock) -> Watchdog:
    config = WatchdogConfig(link_lost_after=LOST, low_battery_percent=20.0, mismatch_grace=GRACE)
    return Watchdog(rig.dispatch, live, live, config=config, clock=clock)


async def flying(rig: Rig) -> tuple[UUID, UUID, UUID]:
    """A started flight: ``(order_id, flight_id, drone_id)``."""
    drone = await rig.add_drone()
    order = await rig.new_order()
    result = await rig.dispatch.dispatch_order(order.id)
    assert result.flight is not None
    return order.id, result.flight.id, drone.id


async def hear(
    live: InMemoryRealtime,
    clock: FakeClock,
    drone_id: UUID,
    *,
    phase: DronePhase = DronePhase.FLYING,
    battery: float = 80.0,
    age: float = 0.0,
) -> None:
    await live.set_state(
        DroneState(
            drone_id=drone_id,
            latitude=12.97,
            longitude=77.59,
            altitude_m=60.0,
            battery_percent=battery,
            phase=phase,
            flight_id=None,
            sent_at=clock() - timedelta(seconds=age),
            received_at=clock() - timedelta(seconds=age),
        )
    )


def kinds(alerts: list[Any]) -> list[AlertKind]:
    return [a.kind for a in alerts]


class TestAHealthyFlight:
    async def test_nothing_to_report(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)

        assert await watchdog.check() == []
        assert await live.active() == []

    async def test_no_flights_and_no_samples(self, watchdog: Watchdog) -> None:
        assert await watchdog.check() == []


class TestLinkLost:
    async def test_a_drone_in_the_air_that_falls_silent_raises_an_alert_with_its_ids(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        order_id, flight_id, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        clock.advance(seconds=20)

        raised = await watchdog.check()

        assert kinds(raised) == [K.LINK_LOST]
        alert = raised[0]
        assert (alert.drone_id, alert.flight_id, alert.order_id) == (drone_id, flight_id, order_id)
        assert "20 s" in alert.message
        assert [a.key for a in await live.active()] == [alert.key]

    async def test_it_is_judged_by_the_platforms_receive_time_not_the_drones_clock(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        state = await live.get_state(drone_id)
        assert state is not None
        # the drone's own clock is an hour behind; it is still sending right now
        await live.set_state(state.model_copy(update={"sent_at": clock() - timedelta(hours=1)}))

        assert await watchdog.check() == []

    @pytest.mark.parametrize(
        ("silent", "expected"), [(14, []), (15, [K.LINK_LOST]), (16, [K.LINK_LOST])]
    )
    async def test_the_threshold(
        self,
        rig: Rig,
        watchdog: Watchdog,
        live: InMemoryRealtime,
        clock: FakeClock,
        silent: int,
        expected: list[AlertKind],
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        clock.advance(seconds=silent)

        assert kinds(await watchdog.check()) == expected

    async def test_a_flight_that_just_started_has_time_to_send_its_first_sample(
        self, rig: Rig, watchdog: Watchdog, clock: FakeClock
    ) -> None:
        await flying(rig)  # never heard from
        clock.advance(seconds=10)
        assert await watchdog.check() == []

        clock.advance(seconds=6)
        assert kinds(await watchdog.check()) == [K.LINK_LOST]

    async def test_a_sample_that_expired_counts_as_silence(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        clock.advance(seconds=90)  # past the live view's 60 s TTL

        assert kinds(await watchdog.check()) == [K.LINK_LOST]

    async def test_it_is_raised_once_not_every_pass(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        clock.advance(seconds=30)

        first = await watchdog.check()
        clock.advance(seconds=5)
        second = await watchdog.check()

        assert kinds(first) == [K.LINK_LOST]
        assert second == []
        assert len(await live.active()) == 1

    async def test_it_clears_and_is_announced_as_resolved_when_the_drone_is_heard_again(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id)
        clock.advance(seconds=30)
        await watchdog.check()

        await hear(live, clock, drone_id)  # back on the air
        await watchdog.check()

        assert await live.active() == []
        newest = (await live.recent())[0]
        assert newest.kind is K.LINK_LOST
        assert newest.resolved_at == clock()

    async def test_a_paused_flight_is_watched_too(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, flight_id, drone_id = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)
        await hear(live, clock, drone_id, phase=DronePhase.FLYING)
        clock.advance(seconds=20)

        assert kinds(await watchdog.check()) == [K.LINK_LOST]

    async def test_a_flight_still_on_the_ground_is_not_watched_for_silence(
        self, rig: Rig, watchdog: Watchdog, clock: FakeClock
    ) -> None:
        await rig.add_drone()
        order = await rig.new_order()
        await rig.dispatch.dispatch_order(order.id, start=False)  # staged, not flying
        clock.advance(minutes=10)

        assert await watchdog.check() == []

    async def test_a_silent_idle_drone_with_no_flight_is_nobodys_business(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        drone = await rig.add_drone()
        await hear(live, clock, drone.id, phase=DronePhase.IDLE)
        clock.advance(seconds=40)

        assert await watchdog.check() == []


class TestLowBattery:
    @pytest.mark.parametrize(
        ("level", "expected"), [(19.9, [K.LOW_BATTERY]), (20.0, []), (80.0, [])]
    )
    async def test_below_the_threshold_while_flying(
        self,
        rig: Rig,
        watchdog: Watchdog,
        live: InMemoryRealtime,
        clock: FakeClock,
        level: float,
        expected: list[AlertKind],
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, battery=level)

        assert kinds(await watchdog.check()) == expected

    async def test_it_clears_when_the_battery_recovers(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, battery=10.0)
        await watchdog.check()

        await hear(live, clock, drone_id, battery=60.0)
        await watchdog.check()

        assert await live.active() == []

    async def test_a_low_battery_on_the_ground_is_not_an_alert(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        drone = await rig.add_drone()
        await hear(live, clock, drone.id, phase=DronePhase.IDLE, battery=5.0)

        assert await watchdog.check() == []


class TestStateMismatch:
    async def test_the_platform_says_flying_but_the_drone_says_idle(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)

        assert await watchdog.check() == []  # a moment of disagreement is normal
        clock.advance(seconds=29)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)
        assert await watchdog.check() == []
        clock.advance(seconds=2)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)

        raised = await watchdog.check()

        assert kinds(raised) == [K.STATE_MISMATCH]
        assert "reports idle" in raised[0].message
        assert raised[0].flight_id is not None

    @pytest.mark.parametrize("phase", [DronePhase.LANDED, DronePhase.READY])
    async def test_landed_or_ready_also_disagree_with_a_flight_in_the_air(
        self,
        rig: Rig,
        watchdog: Watchdog,
        live: InMemoryRealtime,
        clock: FakeClock,
        phase: DronePhase,
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, phase=phase)
        await watchdog.check()
        clock.advance(seconds=31)
        await hear(live, clock, drone_id, phase=phase)

        assert kinds(await watchdog.check()) == [K.STATE_MISMATCH]

    async def test_the_drone_flying_with_no_live_flight_on_the_platform(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        drone = await rig.add_drone()
        await hear(live, clock, drone.id, phase=DronePhase.FLYING)
        await watchdog.check()
        clock.advance(seconds=31)
        await hear(live, clock, drone.id, phase=DronePhase.FLYING)

        raised = await watchdog.check()

        assert kinds(raised) == [K.STATE_MISMATCH]
        assert raised[0].flight_id is None
        assert "no live flight" in raised[0].message

    async def test_a_recalled_drone_flying_home_is_not_a_mismatch(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, flight_id, drone_id = await flying(rig)
        await rig.dispatch.return_to_home(flight_id)
        await hear(live, clock, drone_id, phase=DronePhase.RETURNING)
        await watchdog.check()
        clock.advance(minutes=5)
        await hear(live, clock, drone_id, phase=DronePhase.RETURNING)

        assert await watchdog.check() == []

    async def test_the_grace_clock_restarts_when_the_disagreement_goes_away(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)
        await watchdog.check()
        clock.advance(seconds=25)
        await hear(live, clock, drone_id, phase=DronePhase.FLYING)  # agreement for a moment
        await watchdog.check()
        clock.advance(seconds=10)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)

        assert await watchdog.check() == []  # 10 s of fresh disagreement, not 35

    async def test_it_clears_when_they_agree_again(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)
        await watchdog.check()
        clock.advance(seconds=31)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE)
        await watchdog.check()
        assert len(await live.active()) == 1

        await hear(live, clock, drone_id, phase=DronePhase.FLYING)
        await watchdog.check()

        assert await live.active() == []


class TestAlertsOnly:
    async def test_it_never_sends_a_command_or_changes_a_status(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        """Whatever it finds, it only alerts: recalling a drone is a person's decision."""
        order_id, flight_id, drone_id = await flying(rig)
        await hear(live, clock, drone_id, phase=DronePhase.IDLE, battery=3.0)
        clock.advance(seconds=40)
        sent = list(rig.link.commands)
        trail = len(await rig.trail(order_id))

        raised: set[AlertKind] = set()
        for _ in range(3):
            raised |= {a.kind for a in await watchdog.check()}
            clock.advance(seconds=10)

        assert raised == {K.LINK_LOST, K.LOW_BATTERY}
        assert rig.link.commands == sent
        assert len(await rig.trail(order_id)) == trail
        assert (await rig.dispatch.get_flight(flight_id)).status is FlightStatus.STARTED
        assert (await rig.drone(drone_id)).status is DroneStatus.ON_MISSION

    async def test_alerts_are_broadcast_to_the_fleet(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, battery=5.0)

        await watchdog.check()

        alerts = [e for c, e in live.published if e.kind == "alert"]
        assert [e.data["kind"] for e in alerts] == ["low_battery"]
        assert alerts[0].droppable is False

    async def test_independent_problems_are_independent_alerts(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime, clock: FakeClock
    ) -> None:
        _, _, drone_id = await flying(rig)
        await hear(live, clock, drone_id, battery=5.0)
        clock.advance(seconds=20)

        assert sorted(a.kind.value for a in await watchdog.check()) == ["link_lost", "low_battery"]

    async def test_it_gives_up_a_pass_when_the_live_view_is_down(
        self, rig: Rig, watchdog: Watchdog, live: InMemoryRealtime
    ) -> None:
        await flying(rig)
        live.broken = True

        with pytest.raises(ConnectionError):
            await watchdog.check()  # the periodic runner logs this and tries again next time
