"""Rule 5 enforced by the database: the audit trail is append-only."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.domain.models import EventSource, Order, OrderStatus
from app.services.order_service import OrderService
from tests.integration.conftest import RunSql, RunTx

pytestmark = pytest.mark.integration

S = OrderStatus

INSERT_EVENT = (
    "INSERT INTO audit_events (event_id, order_id, event_type, previous_state, new_state,"
    " occurred_at, source) VALUES (:event_id, :order_id, :event_type, :previous, :new,"
    " now(), 'ops')"
)


async def _audit_snapshot(run_sql: RunSql) -> list[tuple[object, ...]]:
    rows = await run_sql(
        "SELECT seq, event_id, order_id, event_type, previous_state, new_state, occurred_at,"
        " source, metadata::text FROM audit_events ORDER BY seq"
    )
    return [tuple(row) for row in rows]


class TestAuditEventsCannotBeChanged:
    async def test_update_is_rejected(self, created_order: Order, run_sql: RunSql) -> None:
        before = await _audit_snapshot(run_sql)

        with pytest.raises(DBAPIError, match="UPDATE on table audit_events is not allowed"):
            await run_sql("UPDATE audit_events SET source = 'system'")
        with pytest.raises(DBAPIError, match="UPDATE on table audit_events is not allowed"):
            await run_sql("UPDATE audit_events SET new_state = 'delivered'")

        assert await _audit_snapshot(run_sql) == before

    async def test_delete_is_rejected(self, created_order: Order, run_sql: RunSql) -> None:
        before = await _audit_snapshot(run_sql)

        with pytest.raises(DBAPIError, match="DELETE on table audit_events is not allowed"):
            await run_sql("DELETE FROM audit_events")

        assert await _audit_snapshot(run_sql) == before

    async def test_truncate_is_rejected(self, created_order: Order, run_sql: RunSql) -> None:
        before = await _audit_snapshot(run_sql)

        with pytest.raises(DBAPIError, match="TRUNCATE on table audit_events is not allowed"):
            await run_sql("TRUNCATE audit_events")
        with pytest.raises(DBAPIError):
            await run_sql("TRUNCATE orders CASCADE")  # cascades into audit_events

        assert await _audit_snapshot(run_sql) == before
        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1

    async def test_orders_with_an_audit_trail_cannot_be_deleted(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        with pytest.raises(DBAPIError, match="violates foreign key constraint"):
            await run_sql("DELETE FROM orders")

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1

    async def test_a_forbidden_update_rolls_back_the_whole_transaction(
        self, pg_service: OrderService, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        await pg_service.transition(created_order.id, S.CANCELLED, source=EventSource.API)
        before = await _audit_snapshot(run_sql)

        with pytest.raises(DBAPIError, match="UPDATE on table audit_events is not allowed"):
            # A legitimate-looking append, then a forbidden rewrite in the same transaction.
            await run_tx(
                (
                    INSERT_EVENT,
                    {
                        "event_id": uuid4(),
                        "order_id": created_order.id,
                        "event_type": "order_status_changed",
                        "previous": "created",
                        "new": "failed",
                    },
                ),
                ("UPDATE audit_events SET source = 'api'", {}),
            )

        assert await _audit_snapshot(run_sql) == before, "the appended row rolled back too"


class TestAuditEventsAreWrittenCorrectly:
    async def test_rows_are_ordered_by_a_strictly_increasing_seq(
        self, pg_service: OrderService, created_order: Order, run_sql: RunSql
    ) -> None:
        await pg_service.transition(
            created_order.id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=uuid4()
        )
        await pg_service.transition(created_order.id, S.PICKED_UP, source=EventSource.DISPATCH)

        seqs = [row[0] for row in await run_sql("SELECT seq FROM audit_events ORDER BY seq")]

        assert len(seqs) == 3
        assert seqs == sorted(set(seqs))
        trail = await pg_service.get_audit_trail(created_order.id)
        assert [e.new_state for e in trail] == [S.CREATED, S.ASSIGNED, S.PICKED_UP]

    async def test_seq_cannot_be_supplied_by_the_client(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        with pytest.raises(DBAPIError, match='"seq"'):
            await run_sql(
                "INSERT INTO audit_events (event_id, seq, order_id, event_type, previous_state,"
                " new_state, occurred_at, source) VALUES (:event_id, 999, :order_id,"
                " 'order_status_changed', 'created', 'cancelled', now(), 'ops')",
                event_id=uuid4(),
                order_id=created_order.id,
            )

    async def test_recorded_at_is_set_by_the_database(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        row = (await run_sql("SELECT recorded_at, occurred_at FROM audit_events"))[0]
        assert row[0] is not None
        assert row[0].utcoffset() is not None


class TestAuditEventConstraints:
    async def test_an_order_cannot_enter_the_same_state_twice(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        # The order is already 'created' via its order_created event.
        with pytest.raises(DBAPIError, match="uq_audit_events_order_new_state"):
            await run_sql(
                INSERT_EVENT,
                event_id=uuid4(),
                order_id=created_order.id,
                event_type="order_created",
                previous=None,
                new="created",
            )

    async def test_duplicate_event_ids_are_rejected(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        event_id = (await run_sql("SELECT event_id FROM audit_events"))[0][0]
        with pytest.raises(DBAPIError, match="pk_audit_events"):
            await run_sql(
                INSERT_EVENT,
                event_id=event_id,
                order_id=created_order.id,
                event_type="order_status_changed",
                previous="created",
                new="cancelled",
            )

    @pytest.mark.parametrize(
        ("event_type", "previous", "new"),
        [
            ("order_status_changed", None, "cancelled"),  # missing previous state
            ("order_status_changed", "created", None),  # missing new state
            ("order_status_changed", "created", "created"),  # not a change
            ("order_created", "created", "created"),  # creation must have no previous state
            ("order_created", None, "assigned"),  # creation must end in 'created'
            ("order_status_changed", "created", "teleported"),  # unknown state
            ("flight_started", None, None),  # unknown event type (added with dispatch)
        ],
    )
    async def test_inconsistent_events_are_rejected(
        self,
        created_order: Order,
        run_sql: RunSql,
        event_type: str,
        previous: str | None,
        new: str | None,
    ) -> None:
        with pytest.raises(DBAPIError, match="violates check constraint"):
            await run_sql(
                INSERT_EVENT,
                event_id=uuid4(),
                order_id=created_order.id,
                event_type=event_type,
                previous=previous,
                new=new,
            )

    async def test_unknown_sources_are_rejected(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        with pytest.raises(DBAPIError, match="violates check constraint"):
            await run_sql(
                "INSERT INTO audit_events (event_id, order_id, event_type, previous_state,"
                " new_state, occurred_at, source) VALUES (:event_id, :order_id,"
                " 'order_status_changed', 'created', 'cancelled', now(), 'somebody')",
                event_id=uuid4(),
                order_id=created_order.id,
            )

    async def test_events_for_unknown_orders_are_rejected(self, run_sql: RunSql) -> None:
        with pytest.raises(DBAPIError, match="violates foreign key constraint"):
            await run_sql(
                INSERT_EVENT,
                event_id=uuid4(),
                order_id=uuid4(),
                event_type="order_created",
                previous=None,
                new="created",
            )


class TestTransitionTableIsImmutable:
    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO order_transitions VALUES ('created', 'delivered')",
            "UPDATE order_transitions SET to_status = 'delivered' WHERE from_status = 'created'",
            "DELETE FROM order_transitions",
            "TRUNCATE order_transitions",
        ],
    )
    async def test_changes_are_rejected(self, run_sql: RunSql, statement: str) -> None:
        with pytest.raises(DBAPIError, match="not allowed"):
            await run_sql(statement)

        assert (await run_sql("SELECT count(*) FROM order_transitions"))[0][0] == 17
