"""SqlAlchemyOrderStore against PostgreSQL + PostGIS."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.domain.models import EventSource, Location, OrderStatus, Package
from app.domain.order_state_machine import OrderStateMachine, TransitionResult
from app.services.exceptions import ConcurrentUpdateError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.integration.conftest import RunSql

pytestmark = pytest.mark.integration

S = OrderStatus
DRONE = UUID(int=0xD0)


def _haversine_m(a: Location, b: Location) -> float:
    radius = 6_371_008.8
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    d_lat, d_lon = lat2 - lat1, math.radians(b.longitude - a.longitude)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _create_result(
    machine: OrderStateMachine, pickup: Location, dropoff: Location, ext: str
) -> TransitionResult:
    return machine.create(
        client_id="mock",
        external_order_id=ext,
        pickup=pickup,
        dropoff=dropoff,
        package=Package(weight_kg=1.0),
        source=EventSource.API,
    )


async def _insert_then_fail(uow_factory: UnitOfWorkFactory, result: TransitionResult) -> None:
    async with uow_factory() as uow:
        await uow.orders.insert(result)
        raise RuntimeError("boom")


class TestGeography:
    async def test_coordinates_are_stored_as_longitude_latitude_not_swapped(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        order = (
            await OrderService(pg_uow_factory).create_order(
                client_id="mock",
                external_order_id="G-1",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order

        row = (
            await run_sql(
                "SELECT ST_X(pickup::geometry), ST_Y(pickup::geometry), ST_SRID(pickup::geometry),"
                " ST_X(dropoff::geometry), ST_Y(dropoff::geometry) FROM orders WHERE id = :id",
                id=order.id,
            )
        )[0]
        assert row[0] == pickup.longitude  # X is longitude in PostGIS
        assert row[1] == pickup.latitude
        assert row[2] == 4326
        assert (row[3], row[4]) == (dropoff.longitude, dropoff.latitude)

    async def test_postgis_distance_agrees_with_haversine(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        order = (
            await OrderService(pg_uow_factory).create_order(
                client_id="mock",
                external_order_id="G-2",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order

        metres = (
            await run_sql(
                "SELECT ST_Distance(pickup, dropoff) FROM orders WHERE id = :id", id=order.id
            )
        )[0][0]
        assert metres == pytest.approx(_haversine_m(pickup, dropoff), rel=0.01)
        assert 1_000 < metres < 10_000, "a swapped lat/lon would give a wildly different distance"

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(90.0, 180.0), (-90.0, -180.0), (0.0, 0.0), (12.123456789012345, 77.987654321098765)],
    )
    async def test_extreme_and_high_precision_coordinates_round_trip_exactly(
        self, pg_uow_factory: UnitOfWorkFactory, package: Package, lat: float, lon: float
    ) -> None:
        service = OrderService(pg_uow_factory)
        pickup = Location(latitude=lat, longitude=lon)
        dropoff = Location(latitude=lat / 2 + 1, longitude=lon / 2 - 1)

        created = (
            await service.create_order(
                client_id="mock",
                external_order_id="G-3",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order

        loaded = await service.get_order(created.id)
        assert (loaded.pickup, loaded.dropoff) == (pickup, dropoff)


class TestTimestamps:
    async def test_non_utc_timestamps_come_back_as_the_same_instant(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        moment = datetime(2026, 11, 11, 14, 30, 15, 123456, tzinfo=ist)
        machine = OrderStateMachine(clock=lambda: moment)
        service = OrderService(pg_uow_factory, machine)

        created = (
            await service.create_order(
                client_id="mock",
                external_order_id="T-1",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order

        loaded = await service.get_order(created.id)
        assert loaded.created_at == moment
        assert loaded.created_at.utcoffset() is not None
        assert loaded.created_at.astimezone(UTC) == datetime(
            2026, 11, 11, 9, 0, 15, 123456, tzinfo=UTC
        )


class TestIdempotentInsert:
    async def test_a_duplicate_key_inserts_nothing_and_writes_no_audit_row(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
    ) -> None:
        machine = OrderStateMachine()
        first = _create_result(machine, pickup, dropoff, "D-1")
        second = _create_result(machine, pickup, dropoff, "D-1")

        async with pg_uow_factory() as uow:
            assert await uow.orders.insert(first) is True
            assert await uow.orders.insert(second) is False
            await uow.commit()

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1
        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 1
        assert (await run_sql("SELECT order_id FROM audit_events"))[0][0] == first.order.id

    async def test_concurrent_creates_with_one_key_yield_one_order_and_one_audit_row(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        service = OrderService(pg_uow_factory)

        results = await asyncio.gather(
            *(
                service.create_order(
                    client_id="mock",
                    external_order_id="RACE-1",
                    pickup=pickup,
                    dropoff=dropoff,
                    package=package,
                    source=EventSource.API,
                )
                for _ in range(10)
            )
        )

        assert sum(r.created for r in results) == 1
        assert len({r.order.id for r in results}) == 1
        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1
        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 1


class TestAtomicity:
    async def test_an_uncommitted_unit_of_work_leaves_nothing_behind(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
    ) -> None:
        result = _create_result(OrderStateMachine(), pickup, dropoff, "A-1")

        async with pg_uow_factory() as uow:
            assert await uow.orders.insert(result) is True
            # leave the block without committing

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 0
        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 0

    async def test_an_error_after_the_write_rolls_back_order_and_audit_together(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
    ) -> None:
        result = _create_result(OrderStateMachine(), pickup, dropoff, "A-2")

        with pytest.raises(RuntimeError, match="boom"):
            await _insert_then_fail(pg_uow_factory, result)

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 0
        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 0


class TestLockingAndCompareAndSet:
    async def test_for_update_makes_a_second_locker_wait(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        order = (
            await OrderService(pg_uow_factory).create_order(
                client_id="mock",
                external_order_id="L-1",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order

        first, second = pg_uow_factory(), pg_uow_factory()
        async with first, second:
            await first.orders.get(order.id, for_update=True)
            waiting = asyncio.create_task(second.orders.get(order.id, for_update=True))
            await asyncio.sleep(0.5)
            assert not waiting.done(), "the second locker should be blocked on the row lock"

            await first.commit()  # ends the transaction and releases the lock
            loaded = await asyncio.wait_for(waiting, timeout=5)
            assert loaded is not None
            assert loaded.id == order.id

    async def test_a_stale_transition_is_rejected_by_compare_and_set(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        run_sql: RunSql,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        machine = OrderStateMachine()
        service = OrderService(pg_uow_factory, machine)
        order = (
            await service.create_order(
                client_id="mock",
                external_order_id="CAS-1",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order
        # Computed from a copy read BEFORE someone else moves the order.
        stale = machine.transition(order, S.CANCELLED, source=EventSource.API)
        await service.transition(order.id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=DRONE)

        with pytest.raises(ConcurrentUpdateError):
            async with pg_uow_factory() as uow:
                await uow.orders.apply_transition(stale)

        assert (await service.get_order(order.id)).status is S.ASSIGNED
        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == 2


class TestWrongKindOfResult:
    async def test_insert_rejects_a_transition_result(
        self,
        pg_uow_factory: UnitOfWorkFactory,
        pickup: Location,
        dropoff: Location,
        package: Package,
    ) -> None:
        machine = OrderStateMachine()
        created = _create_result(machine, pickup, dropoff, "W-1")
        changed = machine.transition(created.order, S.CANCELLED, source=EventSource.API)

        async with pg_uow_factory() as uow:
            with pytest.raises(ValueError, match="create"):
                await uow.orders.insert(changed)

    async def test_apply_transition_rejects_a_creation_result(
        self, pg_uow_factory: UnitOfWorkFactory, pickup: Location, dropoff: Location
    ) -> None:
        created = _create_result(OrderStateMachine(), pickup, dropoff, "W-2")

        async with pg_uow_factory() as uow:
            with pytest.raises(ValueError, match="transition"):
                await uow.orders.apply_transition(created)
