"""Drone and flight store contracts, run against BOTH the in-memory and PostgreSQL stores.

The ``memory`` variant always runs; ``postgres`` needs PostgreSQL with PostGIS. Identical
assertions on both keep the in-memory fake honest.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.exceptions import (
    DroneValidationError,
    FlightValidationError,
    InvalidDroneTransitionError,
)
from app.domain.flight_state_machine import FlightStateMachine
from app.domain.models import (
    AuditEventType,
    DroneStatus,
    EventSource,
    Flight,
    FlightStatus,
    Location,
    Mission,
    Order,
    OrderStatus,
    Waypoint,
    WaypointKind,
)
from app.services.exceptions import ConcurrentUpdateError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.conftest import DRONE_ID, HOME, ORDER_ID, build_drone, build_mission
from tests.integration.flights import FlightFlow

S = FlightStatus
D = DroneStatus
OTHER_DRONE = UUID(int=0xD1)


@pytest.fixture
async def world(
    uow_factory: UnitOfWorkFactory,
    pickup: Location,
    dropoff: Location,
    make_order: Callable[..., Order],
) -> tuple[UnitOfWorkFactory, Order, Order]:
    """Two stored orders and two registered drones (``DRONE_ID`` and ``OTHER_DRONE``)."""
    from app.domain.models import Package

    service = OrderService(uow_factory)
    orders = []
    for n in (1, 2):
        result = await service.create_order(
            client_id="mock",
            external_order_id=f"W-{n}",
            pickup=pickup,
            dropoff=dropoff,
            package=Package(weight_kg=1.0),
            source=EventSource.API,
        )
        orders.append(result.order)
    async with uow_factory() as uow:
        for drone_id, name in ((DRONE_ID, "alpha"), (OTHER_DRONE, "bravo")):
            assert await uow.drones.add(build_drone(id=drone_id, name=name))
        await uow.commit()
    return uow_factory, orders[0], orders[1]


class TestDroneStore:
    async def test_a_drone_round_trips_exactly(self, uow_factory: UnitOfWorkFactory) -> None:
        drone = build_drone(
            home=Location(latitude=12.123456789012345, longitude=77.987654321098765),
            max_payload_kg=3.25,
            max_range_km=18.5,
            battery_percent=73.5,
            created_at=datetime(2026, 11, 11, 9, 0, 15, 123456, tzinfo=UTC),
            updated_at=datetime(2026, 11, 11, 9, 5, 0, 654321, tzinfo=UTC),
        )
        async with uow_factory() as uow:
            assert await uow.drones.add(drone) is True
            await uow.commit()

        async with uow_factory() as uow:
            assert await uow.drones.get(drone.id) == drone

    async def test_a_missing_drone_is_none(self, uow_factory: UnitOfWorkFactory) -> None:
        async with uow_factory() as uow:
            assert await uow.drones.get(UUID(int=404)) is None

    async def test_a_taken_name_stores_nothing(self, uow_factory: UnitOfWorkFactory) -> None:
        async with uow_factory() as uow:
            assert await uow.drones.add(build_drone(id=UUID(int=1), name="same")) is True
            assert await uow.drones.add(build_drone(id=UUID(int=2), name="same")) is False
            await uow.commit()

        async with uow_factory() as uow:
            assert [d.id for d in await uow.drones.list_all()] == [UUID(int=1)]

    async def test_an_uncommitted_registration_leaves_nothing_behind(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        async with uow_factory() as uow:
            await uow.drones.add(build_drone())

        async with uow_factory() as uow:
            assert await uow.drones.list_all() == []

    async def test_listing_is_ordered_by_name_and_can_filter_by_status(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        async with uow_factory() as uow:
            await uow.drones.add(build_drone(id=UUID(int=1), name="charlie"))
            await uow.drones.add(build_drone(id=UUID(int=2), name="alpha", status=D.OFFLINE))
            await uow.drones.add(build_drone(id=UUID(int=3), name="bravo"))
            await uow.commit()

        async with uow_factory() as uow:
            assert [d.name for d in await uow.drones.list_all()] == ["alpha", "bravo", "charlie"]
            offline = await uow.drones.list_all(status=D.OFFLINE)
            assert [d.name for d in offline] == ["alpha"]

    async def test_only_available_drones_are_listed_as_available(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        async with uow_factory() as uow:
            await uow.drones.add(build_drone(id=UUID(int=3), name="c"))
            await uow.drones.add(build_drone(id=UUID(int=1), name="a"))
            for n, status in ((4, D.ASSIGNED), (5, D.ON_MISSION), (6, D.MAINTENANCE)):
                await uow.drones.add(build_drone(id=UUID(int=n), name=f"x{n}", status=status))
            await uow.commit()

        async with uow_factory() as uow:
            available = await uow.drones.list_available()
            assert [d.id for d in available] == [UUID(int=1), UUID(int=3)]
            assert [d.id for d in await uow.drones.list_available(lock=True)] == [
                UUID(int=1),
                UUID(int=3),
            ]

    async def test_a_status_change_is_stored(self, uow_factory: UnitOfWorkFactory) -> None:
        drone = build_drone()
        async with uow_factory() as uow:
            await uow.drones.add(drone)
            await uow.commit()
        later = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)

        async with uow_factory() as uow:
            await uow.drones.apply_status_change(drone, drone.with_status(D.ASSIGNED, now=later))
            await uow.commit()

        async with uow_factory() as uow:
            stored = await uow.drones.get(drone.id)
            assert stored is not None
            assert (stored.status, stored.updated_at) == (D.ASSIGNED, later)
            assert stored.name == drone.name

    async def test_a_stale_before_is_rejected_by_compare_and_set(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        drone = build_drone()
        async with uow_factory() as uow:
            await uow.drones.add(drone)
            await uow.commit()
        now = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
        async with uow_factory() as uow:  # someone else moves it first
            await uow.drones.apply_status_change(drone, drone.with_status(D.MAINTENANCE, now=now))
            await uow.commit()

        with pytest.raises(ConcurrentUpdateError):
            async with uow_factory() as uow:
                await uow.drones.apply_status_change(drone, drone.with_status(D.ASSIGNED, now=now))

        async with uow_factory() as uow:
            stored = await uow.drones.get(drone.id)
            assert stored is not None
            assert stored.status is D.MAINTENANCE

    async def test_changing_a_drone_that_does_not_exist_is_a_conflict(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        ghost = build_drone(id=UUID(int=404), name="ghost")
        with pytest.raises(ConcurrentUpdateError):
            async with uow_factory() as uow:
                await uow.drones.apply_status_change(
                    ghost, ghost.with_status(D.ASSIGNED, now=ghost.updated_at)
                )

    async def test_an_illegal_edge_is_refused_before_anything_is_written(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        offline = build_drone(status=D.OFFLINE)
        async with uow_factory() as uow:
            await uow.drones.add(offline)
            await uow.commit()
        from dataclasses import replace

        with pytest.raises(InvalidDroneTransitionError):
            async with uow_factory() as uow:
                await uow.drones.apply_status_change(offline, replace(offline, status=D.ON_MISSION))

        async with uow_factory() as uow:
            stored = await uow.drones.get(offline.id)
            assert stored is not None
            assert stored.status is D.OFFLINE

    async def test_changing_anything_but_the_status_is_refused(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        drone = build_drone()
        async with uow_factory() as uow:
            await uow.drones.add(drone)
            await uow.commit()
        from dataclasses import replace

        sneaky = replace(drone.with_status(D.ASSIGNED, now=drone.updated_at), battery_percent=1.0)

        with pytest.raises(DroneValidationError):
            async with uow_factory() as uow:
                await uow.drones.apply_status_change(drone, sneaky)

    async def test_for_update_reads_work(self, uow_factory: UnitOfWorkFactory) -> None:
        async with uow_factory() as uow:
            await uow.drones.add(build_drone())
            await uow.commit()
        async with uow_factory() as uow:
            assert await uow.drones.get(DRONE_ID, for_update=True) is not None


class TestFlightStore:
    async def test_a_planned_flight_round_trips_exactly(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        mission = Mission(
            mission_id=UUID(int=0x77),
            waypoints=(
                Waypoint(WaypointKind.TAKEOFF, HOME, 0.0),
                Waypoint(
                    WaypointKind.DROPOFF,
                    Location(latitude=12.123456789012345, longitude=77.987654321098765),
                    60.123456789,
                ),
                Waypoint(WaypointKind.LAND, HOME, 0.0),
            ),
            cruise_speed_mps=11.75,
            estimated_duration_s=543.21,
        )

        planned = await flow.plan(order.id, DRONE_ID, mission)

        async with factory() as uow:
            stored = await uow.flights.get(planned.id)
        assert stored == planned
        assert stored is not None
        assert stored.mission.checksum == mission.checksum, "floats survive storage exactly"

    async def test_a_missing_flight_is_none(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, _, _ = world
        async with factory() as uow:
            assert await uow.flights.get(UUID(int=404)) is None

    async def test_every_step_of_the_lifecycle_is_stored_faithfully(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        flight = await flow.plan(order.id, DRONE_ID)

        for target in (S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.PAUSED, S.STARTED):
            flight = await flow.advance(flight, target)
            async with factory() as uow:
                assert await uow.flights.get(flight.id) == flight, target

        assert flight.permission is not None
        assert flight.started_at is not None
        flight = await flow.advance(flight, S.COMPLETED)
        async with factory() as uow:
            stored = await uow.flights.get(flight.id)
        assert stored == flight
        assert stored is not None
        assert stored.status is S.COMPLETED
        assert stored.permission is not None
        assert stored.acknowledged_checksum == stored.mission.checksum

    @pytest.mark.parametrize(
        "terminal",
        [S.PERMISSION_DENIED, S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED],
    )
    async def test_ending_early_stores_the_reason_or_denial(
        self, world: tuple[UnitOfWorkFactory, Order, Order], terminal: FlightStatus
    ) -> None:
        factory, order, _ = world

        flight = await FlightFlow(factory).flight_in(terminal, order.id, DRONE_ID)

        async with factory() as uow:
            stored = await uow.flights.get(flight.id)
        assert stored == flight
        assert stored is not None
        if terminal is S.PERMISSION_DENIED:
            assert stored.denial is not None
            assert stored.denial.reason == "test denial"
        else:
            assert stored.failure_reason == "test"

    async def test_flights_of_an_order_are_listed_oldest_first(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, other_order = world
        flow = FlightFlow(factory)
        first = await flow.flight_in(S.CANCELLED, order.id, DRONE_ID)
        second = await flow.flight_in(S.PERMISSION_DENIED, order.id, DRONE_ID)
        third = await flow.plan(order.id, OTHER_DRONE)
        await flow.plan(other_order.id, DRONE_ID)  # belongs to another order

        async with factory() as uow:
            listed = await uow.flights.list_for_order(order.id)

        assert [f.id for f in listed] == [first.id, second.id, third.id]

    async def test_the_live_flight_of_a_drone(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, other_order = world
        flow = FlightFlow(factory)
        async with factory() as uow:
            assert await uow.flights.get_live_for_drone(DRONE_ID) is None

        await flow.flight_in(S.CANCELLED, order.id, DRONE_ID)  # finished flights are not live
        async with factory() as uow:
            assert await uow.flights.get_live_for_drone(DRONE_ID) is None

        live = await flow.plan(other_order.id, DRONE_ID)
        async with factory() as uow:
            assert await uow.flights.get_live_for_drone(DRONE_ID) == live
            assert await uow.flights.get_live_for_drone(OTHER_DRONE) is None  # another drone

        await flow.advance(live, S.CANCELLED)
        async with factory() as uow:
            assert await uow.flights.get_live_for_drone(DRONE_ID) is None

    async def test_a_drone_can_have_only_one_live_flight(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, other_order = world
        flow = FlightFlow(factory)
        await flow.plan(order.id, DRONE_ID)
        rival = flow.machine.create(
            order_id=other_order.id,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )

        async with factory() as uow:
            assert await uow.flights.insert(rival) is False
            await uow.commit()

        async with factory() as uow:
            assert await uow.flights.get(rival.flight.id) is None
            assert await uow.audit.list_for_flight(rival.flight.id) == []

    async def test_an_order_can_have_only_one_live_flight(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        await flow.plan(order.id, DRONE_ID)
        rival = flow.machine.create(
            order_id=order.id,
            drone_id=OTHER_DRONE,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )

        async with factory() as uow:
            assert await uow.flights.insert(rival) is False

    @pytest.mark.parametrize("terminal", [S.CANCELLED, S.PERMISSION_DENIED, S.COMPLETED])
    async def test_once_a_flight_ends_the_drone_and_order_are_free_again(
        self, world: tuple[UnitOfWorkFactory, Order, Order], terminal: FlightStatus
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        await flow.flight_in(terminal, order.id, DRONE_ID)

        again = await flow.plan(order.id, DRONE_ID)

        assert again.status is S.PLANNED

    async def test_a_stale_transition_is_rejected_by_compare_and_set(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        planned = await flow.plan(order.id, DRONE_ID)
        stale = flow.machine.transition(
            planned, S.CANCELLED, source=EventSource.OPS, reason="stale view"
        )
        await flow.advance(planned, S.PERMISSION_GRANTED)  # someone else got there first

        with pytest.raises(ConcurrentUpdateError):
            async with factory() as uow:
                await uow.flights.apply_transition(stale)

        async with factory() as uow:
            stored = await uow.flights.get(planned.id)
            assert stored is not None
            assert stored.status is S.PERMISSION_GRANTED
            assert len(await uow.audit.list_for_flight(planned.id)) == 2

    async def test_an_uncommitted_insert_or_transition_leaves_nothing_behind(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        machine = FlightStateMachine()
        planned = machine.create(
            order_id=order.id,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )

        async with factory() as uow:
            assert await uow.flights.insert(planned) is True
            # no commit

        async with factory() as uow:
            assert await uow.flights.get(planned.flight.id) is None
            assert await uow.audit.list_for_flight(planned.flight.id) == []

    async def test_the_wrong_kind_of_result_is_refused(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        machine = FlightStateMachine()
        created = machine.create(
            order_id=order.id,
            drone_id=DRONE_ID,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        changed = machine.transition(
            created.flight, S.CANCELLED, source=EventSource.OPS, reason="x"
        )

        async with factory() as uow:
            with pytest.raises(ValueError, match="create"):
                await uow.flights.insert(changed)
            with pytest.raises(ValueError, match="transition"):
                await uow.flights.apply_transition(created)

    async def test_for_update_reads_work(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        planned = await FlightFlow(factory).plan(order.id, DRONE_ID)
        async with factory() as uow:
            assert await uow.flights.get(planned.id, for_update=True) == planned


class TestFlightAuditTrail:
    async def test_every_flight_change_leaves_an_event_in_order(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)

        flight = await flow.flight_in(S.COMPLETED, order.id, DRONE_ID)

        async with factory() as uow:
            trail = await uow.audit.list_for_flight(flight.id)
        assert [e.event_type for e in trail] == [
            AuditEventType.FLIGHT_CREATED,
            *[AuditEventType.FLIGHT_STATUS_CHANGED] * 4,
        ]
        assert [e.new_state for e in trail] == [
            S.PLANNED,
            S.PERMISSION_GRANTED,
            S.MISSION_UPLOADED,
            S.STARTED,
            S.COMPLETED,
        ]
        assert [e.previous_state for e in trail[1:]] == [e.new_state for e in trail[:-1]]
        assert {e.flight_id for e in trail} == {flight.id}
        assert {e.drone_id for e in trail} == {DRONE_ID}
        assert all(isinstance(e.new_state, FlightStatus) for e in trail)

    async def test_the_start_event_records_what_the_gate_relied_on(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world

        flight = await FlightFlow(factory).flight_in(S.STARTED, order.id, DRONE_ID)

        async with factory() as uow:
            started = (await uow.audit.list_for_flight(flight.id))[-1]
        assert flight.permission is not None
        assert started.metadata["permission_id"] == flight.permission.permission_id
        assert started.metadata["resumed"] is False
        assert "validated_at" in started.metadata
        assert "permission_valid_until" in started.metadata

    async def test_pause_and_resume_leave_two_started_events(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flow = FlightFlow(factory)
        flight = await flow.flight_in(S.PAUSED, order.id, DRONE_ID)

        flight = await flow.advance(flight, S.STARTED)

        async with factory() as uow:
            trail = await uow.audit.list_for_flight(flight.id)
        started = [e for e in trail if e.new_state is S.STARTED]
        assert len(started) == 2
        assert [bool(e.metadata["resumed"]) for e in started] == [False, True]

    async def test_the_order_trail_holds_order_and_flight_events_together(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, order, _ = world
        flight = await FlightFlow(factory).flight_in(S.PERMISSION_GRANTED, order.id, DRONE_ID)

        async with factory() as uow:
            trail = await uow.audit.list_for_order(order.id)

        assert [e.event_type for e in trail] == [
            AuditEventType.ORDER_CREATED,
            AuditEventType.FLIGHT_CREATED,
            AuditEventType.FLIGHT_STATUS_CHANGED,
        ]
        assert isinstance(trail[0].new_state, OrderStatus)
        assert trail[1].flight_id == flight.id

    async def test_a_flight_with_no_events_yet_has_an_empty_trail(
        self, world: tuple[UnitOfWorkFactory, Order, Order]
    ) -> None:
        factory, _, _ = world
        async with factory() as uow:
            assert await uow.audit.list_for_flight(UUID(int=404)) == []


class TestRehydration:
    async def test_a_corrupt_stored_flight_is_detected_not_trusted(self) -> None:
        """Flight.rehydrate re-validates, so an impossible combination cannot be loaded."""
        with pytest.raises(FlightValidationError):
            Flight.rehydrate(
                id=UUID(int=1),
                order_id=ORDER_ID,
                drone_id=DRONE_ID,
                status=S.STARTED,
                mission=build_mission(),
                created_at=datetime(2026, 11, 11, tzinfo=UTC),
                updated_at=datetime(2026, 11, 11, tzinfo=UTC),
            )
