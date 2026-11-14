"""The simulated drone: the drone side of the protocol, driven by a fake clock."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.domain.models import Location, Mission, Waypoint, WaypointKind
from app.protocol import messages as wire
from app.simulator.drone import Phase, SimulatedDrone
from tests.conftest import DRONE_ID, FLIGHT_ID, HOME, PICKUP, T0, FakeClock, build_mission

E = wire.EventType
FULL_FLIGHT = [
    E.PACKAGE_PICKED_UP,
    E.DEPARTED_PICKUP,
    E.ARRIVING_AT_DROPOFF,
    E.PACKAGE_DELIVERED,
    E.MISSION_COMPLETED,
]
OTHER = UUID(int=0xD1)


class Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> UUID:
        self.n += 1
        return UUID(int=0xE000 + self.n)


def envelope(drone_id: UUID = DRONE_ID, n: int = 1) -> dict[str, Any]:
    return {
        "command_id": UUID(int=0xC000 + n),
        "drone_id": drone_id,
        "sent_at": T0,
        "reply_to": "platform/acks/test",
    }


def upload(mission: Mission | None = None, **kwargs: Any) -> wire.UploadMission:
    wire_mission = wire.WireMission.from_domain(mission or build_mission())
    return wire.UploadMission(**{**envelope(), **kwargs}, mission=wire_mission)


def start(
    clock: FakeClock,
    mission: Mission | None = None,
    *,
    flight_id: UUID = FLIGHT_ID,
    drone_id: UUID = DRONE_ID,
    valid_for: timedelta = timedelta(hours=1),
    checksum: str | None = None,
    n: int = 2,
) -> wire.StartMission:
    mission = mission or build_mission()
    return wire.StartMission(
        **envelope(n=n),
        authorization=wire.WireAuthorization(
            flight_id=flight_id,
            drone_id=drone_id,
            permission_id="PERM-1",
            mission_checksum=checksum or mission.checksum,
            authorized_at=clock(),
            valid_until=clock() + valid_for,
        ),
    )


@pytest.fixture
def drone(clock: FakeClock) -> SimulatedDrone:
    return SimulatedDrone(
        DRONE_ID, home=HOME, speed_factor=1.0, hold_s=10.0, clock=clock, id_factory=Ids()
    )


def launch(drone: SimulatedDrone, clock: FakeClock) -> None:
    assert drone.on_command(upload()).status is wire.AckStatus.ACCEPTED  # type: ignore[union-attr]
    assert drone.on_command(start(clock)).status is wire.AckStatus.ACCEPTED  # type: ignore[union-attr]


def fly(drone: SimulatedDrone, seconds: float, step: float = 10.0) -> list[wire.DroneEvent]:
    events: list[wire.DroneEvent] = []
    for _ in range(int(seconds / step)):
        events += drone.advance(step)
    return events


def types(events: list[wire.DroneEvent]) -> list[E]:
    return [e.type for e in events]


class TestUpload:
    def test_it_accepts_a_mission_and_echoes_the_checksum_it_computed(
        self, drone: SimulatedDrone
    ) -> None:
        ack = drone.on_command(upload())

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert ack.mission_checksum == build_mission().checksum
        assert drone.phase is Phase.READY

    def test_it_refuses_a_mission_that_does_not_match_its_checksum(
        self, drone: SimulatedDrone
    ) -> None:
        wire_mission = wire.WireMission.from_domain(build_mission()).model_copy(
            update={"checksum": "0" * 64}
        )

        ack = drone.on_command(wire.UploadMission(**envelope(), mission=wire_mission))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert ack.reason is not None
        assert "checksum" in ack.reason
        assert drone.phase is Phase.IDLE

    def test_it_refuses_a_structurally_invalid_mission(self, drone: SimulatedDrone) -> None:
        good = wire.WireMission.from_domain(build_mission())
        broken = good.model_copy(update={"waypoints": good.waypoints[:1]})

        ack = drone.on_command(wire.UploadMission(**envelope(), mission=broken))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert ack.reason is not None
        assert "invalid mission" in ack.reason

    def test_it_will_not_take_a_mission_while_airborne(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)

        ack = drone.on_command(upload())

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED

    def test_it_ignores_commands_addressed_to_another_drone(self, drone: SimulatedDrone) -> None:
        ack = drone.on_command(wire.PauseMission(**envelope(OTHER)))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert ack.drone_id == DRONE_ID


class TestStartIsOnlyForAValidAuthorisation:
    def reject(self, drone: SimulatedDrone, command: wire.StartMission, why: str) -> None:
        ack = drone.on_command(command)
        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert ack.reason is not None
        assert why in ack.reason
        assert drone.phase is Phase.READY

    def test_a_valid_authorisation_starts_the_flight(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        drone.on_command(upload())

        ack = drone.on_command(start(clock))

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert drone.phase is Phase.FLYING
        assert drone.flight_id == FLIGHT_ID

    def test_without_a_mission_on_board(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        ack = drone.on_command(start(clock))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert drone.phase is Phase.IDLE

    def test_for_another_drone(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        drone.on_command(upload())
        self.reject(drone, start(clock, drone_id=OTHER), "another drone")

    def test_expired(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        drone.on_command(upload())
        command = start(clock, valid_for=timedelta(seconds=30))
        clock.advance(seconds=30)  # exactly at expiry
        self.reject(drone, command, "expired")

    def test_for_a_different_mission_than_the_one_on_board(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        drone.on_command(upload())
        self.reject(drone, start(clock, checksum="f" * 64), "different mission")

    def test_it_cannot_be_started_twice(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        launch(drone, clock)

        ack = drone.on_command(start(clock, n=3, flight_id=UUID(int=0xF2)))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert drone.flight_id == FLIGHT_ID  # a second authorisation did not hijack the flight


class TestAFullFlight:
    def test_it_reports_the_whole_journey_in_order(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)

        events = fly(drone, 4000)

        assert types(events) == FULL_FLIGHT
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert {e.flight_id for e in events} == {FLIGHT_ID}
        assert len({e.event_id for e in events}) == 5
        assert drone.phase is Phase.IDLE
        assert drone.position == HOME

    @pytest.mark.parametrize("step", [0.05, 1.0, 10.0, 250.0, 100_000.0])
    def test_the_events_do_not_depend_on_how_time_is_sliced(
        self, drone: SimulatedDrone, clock: FakeClock, step: float
    ) -> None:
        launch(drone, clock)
        events: list[wire.DroneEvent] = []

        for _ in range(200_000):
            events += drone.advance(step)
            if drone.phase is Phase.IDLE:
                break

        assert types(events) == FULL_FLIGHT

    def test_it_stops_at_the_pickup_and_the_dropoff_for_the_hold_time(
        self, clock: FakeClock
    ) -> None:
        drone = SimulatedDrone(DRONE_ID, home=HOME, hold_s=100.0, clock=clock, id_factory=Ids())
        launch(drone, clock)

        before = types(fly(drone, 150, step=1.0))  # reaches the pickup (~83 s) and holds
        assert before == [E.PACKAGE_PICKED_UP]
        assert drone.position == PICKUP

        assert types(fly(drone, 100, step=1.0)) == [E.DEPARTED_PICKUP]  # the hold ends ~183 s

    def test_a_faster_clock_flies_the_same_mission_in_less_real_time(
        self, clock: FakeClock
    ) -> None:
        def real_seconds_to_finish(factor: float) -> float:
            drone = SimulatedDrone(
                DRONE_ID, home=HOME, speed_factor=factor, hold_s=10.0, clock=clock, id_factory=Ids()
            )
            launch(drone, clock)
            elapsed = 0.0
            while drone.phase is not Phase.IDLE:
                drone.advance(0.5)
                elapsed += 0.5
            return elapsed

        slow, fast = real_seconds_to_finish(20.0), real_seconds_to_finish(40.0)

        assert fast == pytest.approx(slow / 2, abs=1.0)

    def test_the_next_mission_starts_a_fresh_sequence(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 4000)
        drone.on_command(upload())
        second = UUID(int=0xF2)
        drone.on_command(start(clock, flight_id=second))

        events = fly(drone, 4000)

        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert {e.flight_id for e in events} == {second}

    def test_arriving_is_reported_once_and_before_the_delivery(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)

        events = fly(drone, 4000, step=0.5)

        assert types(events).count(E.ARRIVING_AT_DROPOFF) == 1
        assert types(events).index(E.ARRIVING_AT_DROPOFF) < types(events).index(E.PACKAGE_DELIVERED)

    def test_when_pickup_and_dropoff_are_close_the_order_of_events_still_holds(
        self, clock: FakeClock
    ) -> None:
        near = Location(latitude=PICKUP.latitude + 0.0005, longitude=PICKUP.longitude)
        mission = Mission(
            mission_id=UUID(int=0x61),
            waypoints=(
                Waypoint(WaypointKind.TAKEOFF, HOME, 0.0),
                Waypoint(WaypointKind.PICKUP, PICKUP, 10.0),
                Waypoint(WaypointKind.DROPOFF, near, 10.0),
                Waypoint(WaypointKind.RETURN, HOME, 60.0),
                Waypoint(WaypointKind.LAND, HOME, 0.0),
            ),
            cruise_speed_mps=12.0,
            estimated_duration_s=600.0,
        )
        drone = SimulatedDrone(DRONE_ID, home=HOME, hold_s=5.0, clock=clock, id_factory=Ids())
        drone.on_command(upload(mission))
        drone.on_command(start(clock, mission))

        assert types(fly(drone, 4000)) == FULL_FLIGHT

    def test_a_zero_hold_still_reports_every_event(self, clock: FakeClock) -> None:
        drone = SimulatedDrone(DRONE_ID, home=HOME, hold_s=0.0, clock=clock, id_factory=Ids())
        launch(drone, clock)

        assert types(fly(drone, 4000)) == FULL_FLIGHT


class TestPauseResume:
    def test_a_paused_drone_does_not_move_or_report(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 20)
        ack = drone.on_command(wire.PauseMission(**envelope(n=5)))
        position = drone.position

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert fly(drone, 4000) == []
        assert drone.position == position
        assert drone.phase is Phase.PAUSED

    def test_resuming_needs_an_authorisation_for_the_same_flight(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        drone.on_command(wire.PauseMission(**envelope(n=5)))

        wrong = drone.on_command(start(clock, flight_id=UUID(int=0xF2), n=6))
        assert wrong is not None
        assert wrong.status is wire.AckStatus.REJECTED
        assert drone.phase is Phase.PAUSED

        right = drone.on_command(start(clock, n=7))
        assert right is not None
        assert right.status is wire.AckStatus.ACCEPTED
        assert types(fly(drone, 4000)) == FULL_FLIGHT

    def test_resuming_with_an_expired_authorisation_is_refused(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        drone.on_command(wire.PauseMission(**envelope(n=5)))
        stale = start(clock, valid_for=timedelta(seconds=10), n=6)
        clock.advance(seconds=11)

        ack = drone.on_command(stale)

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert drone.phase is Phase.PAUSED

    def test_pausing_when_not_flying_is_refused(self, drone: SimulatedDrone) -> None:
        ack = drone.on_command(wire.PauseMission(**envelope()))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED


class TestRecallAndLand:
    def test_a_recalled_drone_flies_home_and_reports_docked_without_delivering(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 100)  # on its way to the pickup
        ack = drone.on_command(wire.ReturnToHome(**envelope(n=5)))

        events = fly(drone, 4000)

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert types(events) == [E.DOCKED]
        assert drone.position == HOME
        assert drone.phase is Phase.IDLE

    def test_a_drone_recalled_after_the_pickup_never_delivers(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        before = fly(drone, 300)  # past the pickup, holding or beyond
        drone.on_command(wire.ReturnToHome(**envelope(n=5)))

        after = fly(drone, 4000)

        assert E.PACKAGE_DELIVERED not in types(before + after)
        assert types(after)[-1] is E.DOCKED

    def test_a_paused_drone_can_be_recalled(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        launch(drone, clock)
        drone.on_command(wire.PauseMission(**envelope(n=5)))

        ack = drone.on_command(wire.ReturnToHome(**envelope(n=6)))

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert types(fly(drone, 4000))[-1] is E.DOCKED

    def test_recalling_twice_is_fine(self, drone: SimulatedDrone, clock: FakeClock) -> None:
        launch(drone, clock)
        drone.on_command(wire.ReturnToHome(**envelope(n=5)))

        ack = drone.on_command(wire.ReturnToHome(**envelope(n=6)))

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED

    def test_recalling_an_idle_drone_is_refused(self, drone: SimulatedDrone) -> None:
        ack = drone.on_command(wire.ReturnToHome(**envelope()))

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED

    def test_land_grounds_the_drone_where_it_is(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 100)
        where = drone.position

        ack = drone.on_command(wire.Land(**envelope(n=5)))

        assert ack is not None
        assert ack.status is wire.AckStatus.ACCEPTED
        assert drone.phase is Phase.LANDED
        assert fly(drone, 4000) == []
        assert drone.position == where

    def test_a_grounded_drone_reports_docked_once_it_is_carried_home(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 100)
        drone.on_command(wire.Land(**envelope(n=5)))

        events = drone.dock()

        assert types(events) == [E.DOCKED]
        assert drone.phase is Phase.IDLE
        assert drone.position == HOME
        assert drone.dock() == []  # nothing more to report


class TestMisbehaviourForTests:
    def test_a_fault_is_reported_without_changing_the_flight(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)

        events = drone.fault("GPS_LOSS", "no fix")

        assert types(events) == [E.FAULT]
        assert events[0].detail == {"code": "GPS_LOSS", "message": "no fix"}
        assert drone.phase is Phase.FLYING

    def test_a_silent_command_is_acted_on_but_not_acknowledged(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        drone.on_command(upload())
        drone.silent.add("start_mission")

        ack = drone.on_command(start(clock))

        assert ack is None
        assert drone.phase is Phase.FLYING  # it did start

    def test_a_refused_command_is_rejected_with_the_given_reason(
        self, drone: SimulatedDrone
    ) -> None:
        drone.refuse["upload_mission"] = "storage full"

        ack = drone.on_command(upload())

        assert ack is not None
        assert ack.status is wire.AckStatus.REJECTED
        assert ack.reason == "storage full"
        assert drone.phase is Phase.IDLE

    def test_the_speed_factor_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="speed_factor"):
            SimulatedDrone(DRONE_ID, home=HOME, speed_factor=0)


class TestSamples:
    def test_an_idle_drone_reports_itself_on_the_ground_at_home(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        sample = drone.sample()

        assert (sample.latitude, sample.longitude) == (HOME.latitude, HOME.longitude)
        assert sample.altitude_m == 0.0
        assert sample.battery_percent == 100.0
        assert sample.phase is wire.DronePhase.IDLE
        assert sample.flight_id is None
        assert sample.sent_at == clock()

    def test_it_reports_the_drones_own_phase_and_flight_through_a_flight(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        drone.on_command(upload())
        assert drone.sample().phase is wire.DronePhase.READY
        assert drone.sample().flight_id is None

        drone.on_command(start(clock))
        flying = drone.sample()
        assert flying.phase is wire.DronePhase.FLYING
        assert flying.flight_id == FLIGHT_ID

        drone.on_command(wire.PauseMission(**envelope(n=5)))
        assert drone.sample().phase is wire.DronePhase.PAUSED

        drone.on_command(wire.ReturnToHome(**envelope(n=6)))
        returning = drone.sample()
        assert returning.phase is wire.DronePhase.RETURNING
        assert returning.flight_id == FLIGHT_ID

    def test_a_grounded_drone_still_names_the_flight_it_was_on(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        drone.on_command(wire.Land(**envelope(n=5)))

        landed = drone.sample()

        assert landed.phase is wire.DronePhase.LANDED
        assert landed.flight_id == FLIGHT_ID
        assert landed.altitude_m == 0.0

    def test_the_position_follows_the_drone_and_it_is_off_the_ground_in_flight(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        fly(drone, 30)

        sample = drone.sample()

        assert (sample.latitude, sample.longitude) != (HOME.latitude, HOME.longitude)
        assert sample.altitude_m > 0

    def test_a_sample_is_always_a_valid_protocol_message(
        self, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        launch(drone, clock)
        for _ in range(40):
            drone.advance(30.0)
            assert wire.decode_telemetry(wire.encode(drone.sample())) == drone.sample()


class TestBattery:
    def test_it_drains_in_the_air_and_not_on_the_ground(self, clock: FakeClock) -> None:
        drone = SimulatedDrone(DRONE_ID, home=HOME, battery_drain_per_s=0.05, clock=clock)
        drone.advance(100.0)  # idle: nothing
        assert drone.battery_percent == 100.0

        launch(drone, clock)
        drone.advance(100.0)

        assert drone.battery_percent == pytest.approx(95.0)

    def test_hovering_costs_as_much_as_flying(self, clock: FakeClock) -> None:
        drone = SimulatedDrone(DRONE_ID, home=HOME, battery_drain_per_s=0.05, clock=clock)
        launch(drone, clock)
        drone.on_command(wire.PauseMission(**envelope(n=5)))

        drone.advance(100.0)

        assert drone.battery_percent == pytest.approx(95.0)

    def test_it_never_goes_below_zero(self, clock: FakeClock) -> None:
        drone = SimulatedDrone(
            DRONE_ID, home=HOME, battery_drain_per_s=100.0, hold_s=1e9, clock=clock
        )
        launch(drone, clock)

        drone.advance(50.0)

        assert drone.battery_percent == 0.0

    @pytest.mark.parametrize("how", ["complete", "recall", "carried"])
    def test_a_fresh_battery_when_it_is_back_on_the_dock(self, clock: FakeClock, how: str) -> None:
        drone = SimulatedDrone(DRONE_ID, home=HOME, battery_drain_per_s=0.05, clock=clock)
        launch(drone, clock)
        drone.advance(60.0)
        assert drone.battery_percent < 100.0
        if how == "recall":
            drone.on_command(wire.ReturnToHome(**envelope(n=5)))
        elif how == "carried":
            drone.on_command(wire.Land(**envelope(n=5)))
            drone.dock()
        fly(drone, 4000, step=1.0)

        assert drone.battery_percent == 100.0

    def test_it_starts_at_the_battery_it_is_given(self, clock: FakeClock) -> None:
        assert (
            SimulatedDrone(DRONE_ID, home=HOME, battery_percent=42.0, clock=clock)
            .sample()
            .battery_percent
            == 42.0
        )

    @pytest.mark.parametrize(
        "bad",
        [{"battery_percent": 101.0}, {"battery_percent": -1.0}, {"battery_drain_per_s": -0.1}],
    )
    def test_impossible_batteries_are_refused(self, bad: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="battery"):
            SimulatedDrone(DRONE_ID, home=HOME, **bad)
