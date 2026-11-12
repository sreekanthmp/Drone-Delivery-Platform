"""A drone can never be double-booked, however many dispatchers race (PostgreSQL only)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.dispatch.assignment import select_drone
from app.domain.flight_state_machine import FlightStateMachine
from app.domain.models import DroneStatus, EventSource, Location, Order, Package
from app.services.exceptions import ConcurrentUpdateError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.conftest import DRONE_ID, build_drone, build_mission
from tests.integration.conftest import RegisterDrone, RunSql

pytestmark = pytest.mark.integration

D = DroneStatus


def now() -> datetime:
    return datetime.now(UTC)


async def reserve_with_a_row_lock(factory: UnitOfWorkFactory, drone_id: UUID) -> bool:
    """Lock the drone, check it is free, mark it assigned. True if this caller got it."""
    async with factory() as uow:
        drone = await uow.drones.get(drone_id, for_update=True)
        if drone is None or drone.status is not D.AVAILABLE:
            return False
        await uow.drones.apply_status_change(drone, drone.with_status(D.ASSIGNED, now=now()))
        await uow.commit()
        return True


@pytest.fixture
async def orders(pg_service: OrderService, pickup: Location, dropoff: Location) -> list[Order]:
    created = []
    for n in range(6):
        result = await pg_service.create_order(
            client_id="mock",
            external_order_id=f"RACE-{n}",
            pickup=pickup,
            dropoff=dropoff,
            package=Package(weight_kg=1.0),
            source=EventSource.API,
        )
        created.append(result.order)
    return created


class TestDroneReservation:
    async def test_with_a_row_lock_exactly_one_of_many_racers_gets_the_drone(
        self, pg_uow_factory: UnitOfWorkFactory, register_drone: RegisterDrone, run_sql: RunSql
    ) -> None:
        await register_drone(DRONE_ID)

        results = await asyncio.gather(
            *(reserve_with_a_row_lock(pg_uow_factory, DRONE_ID) for _ in range(8))
        )

        assert sum(results) == 1
        assert (await run_sql("SELECT status FROM drones"))[0][0] == "assigned"

    async def test_even_without_a_lock_compare_and_set_lets_exactly_one_win(
        self, pg_uow_factory: UnitOfWorkFactory, register_drone: RegisterDrone, run_sql: RunSql
    ) -> None:
        """Everyone reads 'available' first (a barrier makes sure), then all try to book it."""
        await register_drone(DRONE_ID)
        racers = 6
        barrier = asyncio.Barrier(racers)

        async def racer() -> str:
            async with pg_uow_factory() as uow:
                drone = await uow.drones.get(DRONE_ID)  # no lock
                assert drone is not None
                assert drone.status is D.AVAILABLE
                await barrier.wait()  # everyone has seen "available"
                try:
                    await uow.drones.apply_status_change(
                        drone, drone.with_status(D.ASSIGNED, now=now())
                    )
                    await uow.commit()
                except ConcurrentUpdateError:
                    return "lost"
                return "won"

        results = await asyncio.gather(*(racer() for _ in range(racers)))

        assert results.count("won") == 1
        assert results.count("lost") == racers - 1
        assert (await run_sql("SELECT status FROM drones"))[0][0] == "assigned"


class TestSkipLocked:
    async def test_a_dispatcher_skips_drones_another_transaction_has_locked(
        self, pg_uow_factory: UnitOfWorkFactory
    ) -> None:
        async with pg_uow_factory() as setup:
            await setup.drones.add(build_drone(id=UUID(int=1), name="a"))
            await setup.drones.add(build_drone(id=UUID(int=2), name="b"))
            await setup.commit()

        first, second = pg_uow_factory(), pg_uow_factory()
        async with first, second:
            await first.drones.get(UUID(int=1), for_update=True)  # holds the row lock on "a"

            locked = await second.drones.list_available(lock=True)
            unlocked = await second.drones.list_available()

            assert [d.id for d in locked] == [UUID(int=2)], (
                "the locked drone is skipped, not waited for"
            )
            assert {d.id for d in unlocked} == {UUID(int=1), UUID(int=2)}

    async def test_once_the_first_transaction_finishes_the_drone_is_visible_again(
        self, pg_uow_factory: UnitOfWorkFactory
    ) -> None:
        async with pg_uow_factory() as setup:
            await setup.drones.add(build_drone(id=UUID(int=1), name="a"))
            await setup.commit()

        first, second = pg_uow_factory(), pg_uow_factory()
        async with first, second:
            await first.drones.get(UUID(int=1), for_update=True)
            assert await second.drones.list_available(lock=True) == []

            await first.commit()  # releases the lock

            assert [d.id for d in await second.drones.list_available(lock=True)] == [UUID(int=1)]


class TestOneLiveFlightPerDroneAndOrder:
    async def test_two_orders_racing_for_one_drone_only_one_flight_is_planned(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        register_drone: RegisterDrone,
        orders: list[Order],
        run_sql: RunSql,
    ) -> None:
        await register_drone(DRONE_ID)
        machine = FlightStateMachine()

        async def plan(order: Order) -> bool:
            result = machine.create(
                order_id=order.id,
                drone_id=DRONE_ID,
                mission=build_mission(),
                source=EventSource.DISPATCH,
            )
            async with pg_uow_factory() as uow:
                planned = await uow.flights.insert(result)
                await uow.commit()
                return planned

        results = await asyncio.gather(*(plan(order) for order in orders))

        assert sum(results) == 1
        assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 1
        flight_events = await run_sql(
            "SELECT count(*) FROM audit_events WHERE event_type = 'flight_created'"
        )
        assert flight_events[0][0] == 1, "a refused flight leaves no audit event"

    async def test_one_order_racing_for_several_drones_only_one_flight_is_planned(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        register_drone: RegisterDrone,
        orders: list[Order],
        run_sql: RunSql,
    ) -> None:
        drones = [await register_drone(UUID(int=n)) for n in range(1, 7)]
        machine = FlightStateMachine()

        async def plan(drone_id: UUID) -> bool:
            result = machine.create(
                order_id=orders[0].id,
                drone_id=drone_id,
                mission=build_mission(),
                source=EventSource.DISPATCH,
            )
            async with pg_uow_factory() as uow:
                planned = await uow.flights.insert(result)
                await uow.commit()
                return planned

        results = await asyncio.gather(*(plan(drone) for drone in drones))

        assert sum(results) == 1
        assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 1


class TestSelectingAndReservingTogether:
    """Dispatch's building blocks, composed: lock candidates, choose, reserve, plan a flight."""

    async def test_many_concurrent_dispatchers_never_book_a_drone_twice(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        register_drone: RegisterDrone,
        orders: list[Order],
        run_sql: RunSql,
    ) -> None:
        for n in range(1, 4):  # three drones for six orders
            await register_drone(UUID(int=n))
        machine = FlightStateMachine()

        async def dispatch(order: Order) -> UUID | None:
            async with pg_uow_factory() as uow:
                candidates = await uow.drones.list_available(lock=True)
                choice = select_drone(order, candidates).choice
                if choice is None:
                    return None
                await uow.drones.apply_status_change(
                    choice.drone, choice.drone.with_status(D.ASSIGNED, now=now())
                )
                result = machine.create(
                    order_id=order.id,
                    drone_id=choice.drone.id,
                    mission=build_mission(),
                    source=EventSource.DISPATCH,
                )
                assert await uow.flights.insert(result), (
                    "a locked, reserved drone has no live flight"
                )
                await uow.commit()
                return choice.drone.id

        outcomes = await asyncio.gather(*(dispatch(order) for order in orders))

        booked: list[Any] = [drone for drone in outcomes if drone is not None]
        assert booked, "at least one dispatcher must have succeeded"
        assert len(booked) <= 3
        assert len(set(booked)) == len(booked), "no drone was given to two orders"
        live = await run_sql("SELECT drone_id, count(*) FROM flights GROUP BY drone_id")
        assert all(row[1] == 1 for row in live)
        assigned = await run_sql("SELECT count(*) FROM drones WHERE status = 'assigned'")
        assert assigned[0][0] == len(booked)
        flights = await run_sql("SELECT count(*) FROM flights")
        assert flights[0][0] == len(booked)
