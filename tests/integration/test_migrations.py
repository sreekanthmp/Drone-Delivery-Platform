"""Alembic migration: schema, guards, downgrade, and drift against the models."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from app.db.models import AuditEventRecord, OrderRecord  # noqa: F401 - registers the tables
from app.domain.order_state_machine import ALLOWED_TRANSITIONS
from tests.integration.conftest import RunSql, alembic_config

pytestmark = pytest.mark.integration


async def _names(run_sql: RunSql, query: str) -> set[str]:
    return {row[0] for row in await run_sql(query)}


async def test_creates_the_expected_tables(run_sql: RunSql) -> None:
    tables = await _names(run_sql, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert {"orders", "audit_events", "order_transitions", "alembic_version"} <= tables


async def test_postgis_is_enabled(run_sql: RunSql) -> None:
    rows = await run_sql("SELECT postgis_version()")
    assert rows[0][0].startswith("3.")


async def test_location_columns_are_geography_points_with_gist_indexes(run_sql: RunSql) -> None:
    columns = await run_sql(
        "SELECT f_geography_column, type, srid FROM geography_columns WHERE f_table_name = 'orders'"
    )
    assert {(c[0], c[1], c[2]) for c in columns} == {
        ("pickup", "Point", 4326),
        ("dropoff", "Point", 4326),
    }
    indexes = await run_sql("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'orders'")
    gist = {name for name, definition in indexes if "USING gist" in definition}
    assert gist == {"ix_orders_pickup", "ix_orders_dropoff"}


async def test_all_guard_triggers_exist_and_the_lifecycle_trigger_is_deferred(
    run_sql: RunSql,
) -> None:
    rows = await run_sql(
        "SELECT tgname, tgrelid::regclass::text, tgdeferrable, tginitdeferred"
        " FROM pg_trigger WHERE NOT tgisinternal"
    )
    triggers = {name: (table, deferrable, deferred) for name, table, deferrable, deferred in rows}
    assert triggers == {
        "audit_events_reject_update_delete": ("audit_events", False, False),
        "audit_events_reject_truncate": ("audit_events", False, False),
        "order_transitions_reject_changes": ("order_transitions", False, False),
        "order_transitions_reject_truncate": ("order_transitions", False, False),
        "orders_enforce_lifecycle": ("orders", True, True),
    }


async def test_seeded_transitions_match_the_python_state_machine(run_sql: RunSql) -> None:
    rows = await run_sql("SELECT from_status, to_status FROM order_transitions")
    in_db = {(src, dst) for src, dst in rows}
    in_python = {(s.value, t.value) for s, targets in ALLOWED_TRANSITIONS.items() for t in targets}
    assert in_db == in_python
    assert len(rows) == 17


async def test_models_and_migration_agree(engine: AsyncEngine) -> None:
    """Autogenerate finds nothing to change: the ORM models describe the migrated schema."""

    def diff(connection: Any) -> list[Any]:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": False},
        )
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as connection:
        differences = await connection.run_sync(diff)

    # Two tables legitimately exist only in the database: PostGIS's own ``spatial_ref_sys``,
    # and ``order_transitions``, reference data that is deliberately not an ORM model (only
    # the migration and the lifecycle trigger touch it). Any other difference is real drift.
    only_in_database = {"spatial_ref_sys", "order_transitions"}
    removed = {d[1].name for d in differences if d[0] == "remove_table"}
    unexpected = [
        d for d in differences if not (d[0] == "remove_table" and d[1].name in only_in_database)
    ]
    assert unexpected == []
    assert removed == only_in_database, "the comparison ran and saw the database-only tables"


async def test_check_constraints_reject_bad_rows(run_sql: RunSql) -> None:
    base = (
        "INSERT INTO orders (id, client_id, external_order_id, pickup, dropoff,"
        " package_weight_kg, status, created_at, updated_at) VALUES"
        " (:id, :client, 'X', ST_GeogFromText('POINT(1 1)'), ST_GeogFromText('POINT(2 2)'),"
        " :weight, :status, now(), now())"
    )
    good = {"id": uuid4(), "client": "c", "weight": 1.0, "status": "created"}
    for override in (
        {"weight": 0.0},
        {"weight": -1.0},
        {"client": "   "},
        {"status": "teleported"},
    ):
        with pytest.raises(DBAPIError, match="violates check constraint"):
            await run_sql(base, **{**good, **override, "id": uuid4()})


async def test_downgrade_removes_everything_and_upgrade_restores_it(
    database_url: str, run_sql: RunSql
) -> None:
    config = alembic_config(database_url)

    await asyncio.to_thread(command.downgrade, config, "base")
    tables = await _names(run_sql, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert not {"orders", "audit_events", "order_transitions"} & tables
    functions = await _names(
        run_sql,
        "SELECT proname FROM pg_proc WHERE proname IN"
        " ('reject_row_mutation', 'orders_enforce_lifecycle')",
    )
    assert functions == set()

    await asyncio.to_thread(command.upgrade, config, "head")
    tables = await _names(run_sql, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert {"orders", "audit_events", "order_transitions"} <= tables
    assert (await run_sql("SELECT count(*) FROM order_transitions"))[0][0] == 17
