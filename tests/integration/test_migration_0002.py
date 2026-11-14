"""Migration 0002 (drones, flights, flight guard): schema details and its two refusals."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from app.domain.flight_state_machine import ALLOWED_FLIGHT_TRANSITIONS
from app.domain.models import FlightStatus, Order
from tests.conftest import DRONE_ID
from tests.integration.conftest import RegisterDrone, RunSql, RunTx, alembic_config
from tests.integration.flights import FlightFlow

pytestmark = pytest.mark.integration


def head_revision(database_url: str) -> str:
    """The newest migration: these tests start from it, so they do not name it."""
    head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
    assert head is not None
    return head


async def test_the_flight_edge_table_matches_the_python_state_machine(run_sql: RunSql) -> None:
    rows = await run_sql("SELECT from_status, to_status FROM flight_transitions")

    in_db = {(src, dst) for src, dst in rows}
    in_python = {
        (s.value, t.value) for s, targets in ALLOWED_FLIGHT_TRANSITIONS.items() for t in targets
    }
    assert in_db == in_python
    assert len(rows) == 13


async def test_live_flights_are_unique_per_drone_and_per_order_via_partial_indexes(
    run_sql: RunSql,
) -> None:
    rows = await run_sql("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'flights'")
    definitions = dict(rows)

    for name in ("uq_flights_live_per_drone", "uq_flights_live_per_order"):
        definition = definitions[name]
        assert "UNIQUE INDEX" in definition
        assert "WHERE" in definition
        for live in ("planned", "permission_granted", "mission_uploaded", "started", "paused"):
            assert f"'{live}'" in definition
        for finished in ("completed", "cancelled", "aborted", "upload_failed", "permission_denied"):
            assert f"'{finished}'" not in definition


async def test_drone_homes_have_a_spatial_index(run_sql: RunSql) -> None:
    rows = await run_sql("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_drones_home'")
    assert "USING gist" in rows[0][0]


async def test_the_drone_home_is_a_geography_point(run_sql: RunSql) -> None:
    rows = await run_sql(
        "SELECT type, srid FROM geography_columns WHERE f_table_name = 'drones'"
        " AND f_geography_column = 'home'"
    )
    assert [(r[0], r[1]) for r in rows] == [("Point", 4326)]


async def test_every_foreign_key_into_the_fleet_and_flights_is_restrict(run_sql: RunSql) -> None:
    rows = await run_sql(
        "SELECT conname, confdeltype::text FROM pg_constraint WHERE contype = 'f'"
        " AND conname IN ('fk_orders_drone_id_drones', 'fk_flights_order_id_orders',"
        " 'fk_flights_drone_id_drones', 'fk_audit_events_flight_id_flights')"
    )
    assert {r[0]: r[1] for r in rows} == {
        "fk_orders_drone_id_drones": "r",
        "fk_flights_order_id_orders": "r",
        "fk_flights_drone_id_drones": "r",
        "fk_audit_events_flight_id_flights": "r",
    }


async def test_the_database_has_exactly_the_check_constraints_the_models_declare(
    run_sql: RunSql,
) -> None:
    from sqlalchemy import CheckConstraint

    from app.db.models import DroneRecord, FlightRecord

    rows = await run_sql(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'c'"
        " AND conrelid::regclass::text IN ('drones', 'flights')"
    )

    for table, model in (("drones", DroneRecord), ("flights", FlightRecord)):
        in_db = {name for t, name in rows if t == table}
        in_model = {
            str(c.name) for c in model.__table__.constraints if isinstance(c, CheckConstraint)
        }
        assert in_db == in_model, table
        assert in_db, f"{table} should have CHECK constraints"


async def test_the_downgrade_refuses_to_delete_flight_audit_events(
    database_url: str,
    pg_uow_factory: Any,
    created_order: Order,
    register_drone: RegisterDrone,
    run_sql: RunSql,
) -> None:
    await register_drone(DRONE_ID)
    flight = await FlightFlow(pg_uow_factory).flight_in(
        FlightStatus.CANCELLED, created_order.id, DRONE_ID
    )
    events_before = (await run_sql("SELECT count(*) FROM audit_events"))[0][0]

    with pytest.raises(RuntimeError, match="append-only"):
        await asyncio.to_thread(command.downgrade, alembic_config(database_url), "0001")

    # The refused migration changed nothing.
    assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == head_revision(
        database_url
    )
    assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == events_before
    assert (await run_sql("SELECT status FROM flights WHERE id = :id", id=flight.id))[0][
        0
    ] == "cancelled"


async def test_the_downgrade_works_when_there_are_no_flight_events(
    database_url: str, run_sql: RunSql
) -> None:
    await asyncio.to_thread(command.downgrade, alembic_config(database_url), "0001")

    assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == "0001"
    tables = {
        r[0] for r in await run_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert not {"drones", "flights", "flight_transitions"} & tables
    columns = {
        r[0]
        for r in await run_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_events'"
        )
    }
    assert "flight_id" not in columns


async def test_the_original_audit_checks_are_restored_by_the_downgrade(
    database_url: str, run_sql: RunSql, run_tx: RunTx, created_order: Order
) -> None:
    await asyncio.to_thread(command.downgrade, alembic_config(database_url), "0001")

    # The 0001 rule again: an order_status_changed event with no previous state is refused.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="type_matches_states"):
        await run_tx(
            (
                "INSERT INTO audit_events (event_id, order_id, event_type, previous_state,"
                " new_state, occurred_at, source) VALUES (:e, :o, 'order_status_changed',"
                " NULL, 'cancelled', now(), 'ops')",
                {"e": uuid4(), "o": created_order.id},
            )
        )


async def test_the_upgrade_refuses_orders_that_reference_unregistered_drones(
    database_url: str, run_sql: RunSql, run_tx: RunTx
) -> None:
    config = alembic_config(database_url)
    await asyncio.to_thread(command.downgrade, config, "0001")
    order_id, stray_drone = uuid4(), UUID(int=0x5757)
    # In the 0001 schema any UUID was accepted as a drone id.
    await run_tx(
        (
            "INSERT INTO orders (id, client_id, external_order_id, pickup, dropoff,"
            " package_weight_kg, status, drone_id, created_at, updated_at) VALUES (:id, 'raw',"
            " 'e', ST_GeogFromText('POINT(1 1)'), ST_GeogFromText('POINT(2 2)'), 1.0,"
            " 'created', :drone, now(), now())",
            {"id": order_id, "drone": stray_drone},
        ),
        (
            "INSERT INTO audit_events (event_id, order_id, event_type, previous_state, new_state,"
            " occurred_at, source) VALUES (:e, :o, 'order_created', NULL, 'created', now(), 'ops')",
            {"e": uuid4(), "o": order_id},
        ),
    )

    with pytest.raises(RuntimeError, match="not registered"):
        await asyncio.to_thread(command.upgrade, config, "head")

    assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == "0001"

    # Clearing the stray reference lets the migration through.
    await run_sql("UPDATE orders SET drone_id = NULL")
    await asyncio.to_thread(command.upgrade, config, "head")
    assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == head_revision(
        database_url
    )
