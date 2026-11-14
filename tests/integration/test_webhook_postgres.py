"""Webhook deliveries in PostgreSQL: concurrency, the database's own guards, and the migration.

The contract tests in ``test_webhook_store.py`` cover behaviour on both backends. This file is
what only the real database can show: concurrent senders, CHECK and foreign-key constraints,
the append-only attempt history, and the migration's refusals.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy.exc import DBAPIError

from app.domain.models import AttemptOutcome, DeliveryAttempt, EventSource, Location, Order, Package
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory
from tests.integration.conftest import RunSql, RunTx, alembic_config

pytestmark = pytest.mark.integration

LEASE = timedelta(seconds=60)


def soon() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


async def make_order(service: OrderService, external_id: str, *, cancel: bool = False) -> Order:
    from app.domain.models import OrderStatus

    created = await service.create_order(
        client_id="mock",
        external_order_id=external_id,
        pickup=Location(latitude=12.9716, longitude=77.5946),
        dropoff=Location(latitude=12.9352, longitude=77.6245),
        package=Package(weight_kg=1.0),
        source=EventSource.API,
    )
    if cancel:
        return await service.transition(
            created.order.id, OrderStatus.CANCELLED, source=EventSource.API
        )
    return created.order


class TestConcurrentSenders:
    async def test_two_senders_claim_different_deliveries(
        self, pg_service: OrderService, pg_uow_factory: UnitOfWorkFactory
    ) -> None:
        for n in range(10):
            await make_order(pg_service, f"ORD-{n}")
        now = soon()

        async with pg_uow_factory() as a, pg_uow_factory() as b:
            first = await a.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=5)
            # ``a`` has not committed: its rows are locked, and ``b`` must skip them, not wait.
            async with asyncio.timeout(5):
                second = await b.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=10)
            await a.commit()
            await b.commit()

        assert len(first) == 5
        assert len(second) == 5
        assert {d.event_id for d in first}.isdisjoint({d.event_id for d in second})

    async def test_a_locked_first_update_keeps_the_second_back(
        self, pg_service: OrderService, pg_uow_factory: UnitOfWorkFactory
    ) -> None:
        busy = await make_order(pg_service, "BUSY", cancel=True)  # two updates
        calm = await make_order(pg_service, "CALM")
        now = soon()

        async with pg_uow_factory() as a, pg_uow_factory() as b:
            (first,) = await a.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=1)
            second = await b.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=10)
            await a.commit()
            await b.commit()

        assert first.order_id == busy.id
        assert [d.order_id for d in second] == [calm.id]  # BUSY's second update stayed behind

    async def test_many_claimers_never_get_the_same_delivery(
        self, pg_service: OrderService, pg_uow_factory: UnitOfWorkFactory
    ) -> None:
        for n in range(30):
            await make_order(pg_service, f"ORD-{n}")
        now = soon()

        async def claimer() -> list[Any]:
            async with pg_uow_factory() as uow:
                claimed = await uow.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=4)
                await uow.commit()
                return [d.event_id for d in claimed]

        batches = await asyncio.gather(*(claimer() for _ in range(12)))

        flat = [event_id for batch in batches for event_id in batch]
        assert len(flat) == len(set(flat))  # nobody got the same one twice
        assert len(flat) == 30  # and none were left behind


class TestTheDatabaseGuards:
    @pytest.fixture
    async def delivery_id(self, pg_service: OrderService) -> Any:
        order = await make_order(pg_service, "ORD-1")
        (event,) = await pg_service.get_audit_trail(order.id)
        return event.event_id

    @pytest.mark.parametrize(
        ("change", "complaint"),
        [
            ("status = 'bogus'", "status_valid"),
            ("client_id = ''", "client_id_not_empty"),
            ("round_start = 5", "attempts_valid"),
            ("attempts = -1", "attempts_valid"),
            ("last_status_code = 42", "status_code_valid"),
            ("last_status_code = 600", "status_code_valid"),
            ("status = 'delivered'", "delivered_at_matches"),
            ("delivered_at = now()", "delivered_at_matches"),
            ("status = 'failed', lease_until = now()", "lease_only_pending"),
        ],
    )
    async def test_nonsense_is_refused_by_a_check_constraint(
        self, run_sql: RunSql, delivery_id: Any, change: str, complaint: str
    ) -> None:
        with pytest.raises(DBAPIError, match=complaint):
            await run_sql(
                f"UPDATE webhook_deliveries SET {change} WHERE event_id = :id", id=delivery_id
            )

    async def test_a_delivery_needs_a_real_audit_event(
        self, run_sql: RunSql, delivery_id: Any
    ) -> None:
        with pytest.raises(DBAPIError, match="fk_webhook_deliveries_event_id_audit_events"):
            await run_sql(
                "INSERT INTO webhook_deliveries (event_id, client_id, order_id, status, attempts,"
                " round_start, next_attempt_at, created_at, updated_at)"
                " SELECT :e, client_id, order_id, status, attempts, round_start,"
                " next_attempt_at, created_at, updated_at FROM webhook_deliveries",
                e=uuid4(),
            )

    async def test_a_delivery_needs_a_real_order(self, run_tx: RunTx, delivery_id: Any) -> None:
        with pytest.raises(DBAPIError, match="fk_webhook_deliveries_order_id_orders"):
            await run_tx(
                ("DELETE FROM webhook_deliveries WHERE event_id = :id", {"id": delivery_id}),
                (
                    "INSERT INTO webhook_deliveries (event_id, client_id, order_id, status,"
                    " attempts, round_start, next_attempt_at, created_at, updated_at)"
                    " VALUES (:id, 'mock', :order, 'pending', 0, 0, now(), now(), now())",
                    {"id": delivery_id, "order": uuid4()},
                ),
            )

    async def test_an_event_has_one_delivery(self, run_sql: RunSql, delivery_id: Any) -> None:
        with pytest.raises(DBAPIError, match="pk_webhook_deliveries"):
            await run_sql(
                "INSERT INTO webhook_deliveries (event_id, client_id, order_id, status, attempts,"
                " round_start, next_attempt_at, created_at, updated_at)"
                " SELECT event_id, client_id, order_id, status, attempts, round_start,"
                " next_attempt_at, created_at, updated_at FROM webhook_deliveries"
            )

    async def test_the_insertion_order_cannot_be_chosen(
        self, run_sql: RunSql, delivery_id: Any
    ) -> None:
        with pytest.raises(DBAPIError, match="can only be updated to DEFAULT"):
            await run_sql("UPDATE webhook_deliveries SET seq = 999")

    async def test_the_sender_scans_pending_ones_through_a_partial_index(
        self, run_sql: RunSql
    ) -> None:
        rows = await run_sql(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_webhook_deliveries_due'"
        )
        definition = rows[0][0]
        assert "next_attempt_at" in definition
        assert "WHERE (status = 'pending'::text)" in definition


class TestTheAttemptHistoryIsAppendOnly:
    @pytest.fixture
    async def recorded(self, pg_service: OrderService, pg_uow_factory: UnitOfWorkFactory) -> Any:
        order = await make_order(pg_service, "ORD-1")
        now = soon()
        async with pg_uow_factory() as uow:
            (taken,) = await uow.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=1)
            attempt = DeliveryAttempt(
                taken.event_id, 1, now, 40, AttemptOutcome.RETRY, status_code=503, error="HTTP 503"
            )
            assert await uow.webhooks.apply(
                taken, taken.retry(now=now, at=now, status_code=503, error="HTTP 503"), attempt
            )
            await uow.commit()
        return order, taken.event_id

    async def test_update_is_rejected(self, run_sql: RunSql, recorded: Any) -> None:
        with pytest.raises(DBAPIError, match="UPDATE on table webhook_attempts is not allowed"):
            await run_sql("UPDATE webhook_attempts SET outcome = 'delivered'")

    async def test_delete_is_rejected(self, run_sql: RunSql, recorded: Any) -> None:
        with pytest.raises(DBAPIError, match="DELETE on table webhook_attempts is not allowed"):
            await run_sql("DELETE FROM webhook_attempts")

    async def test_truncate_is_rejected(self, run_sql: RunSql, recorded: Any) -> None:
        with pytest.raises(DBAPIError, match="TRUNCATE on table webhook_attempts is not allowed"):
            await run_sql("TRUNCATE webhook_attempts")

    async def test_a_delivery_with_attempts_cannot_be_deleted(
        self, run_sql: RunSql, recorded: Any
    ) -> None:
        with pytest.raises(DBAPIError, match="fk_webhook_attempts_event_id_webhook_deliveries"):
            await run_sql("DELETE FROM webhook_deliveries")

        assert (await run_sql("SELECT count(*) FROM webhook_attempts"))[0][0] == 1

    @pytest.mark.parametrize(
        ("columns", "complaint"),
        [
            ("attempt_no = 1", "uq_webhook_attempts_event_attempt"),  # a repeat
            ("attempt_no = 0", "attempt_no_valid"),
            ("duration_ms = -1", "duration_valid"),
            ("status_code = 42", "status_code_valid"),
            ("outcome = 'maybe'", "outcome_valid"),
        ],
    )
    async def test_an_attempt_must_make_sense(
        self, run_sql: RunSql, recorded: Any, columns: str, complaint: str
    ) -> None:
        base = {
            "attempt_no": "2",
            "duration_ms": "1",
            "status_code": "NULL",
            "outcome": "'failed'",
        }
        key = columns.split(" = ")[0]
        base[key] = columns.split(" = ")[1]
        with pytest.raises(DBAPIError, match=complaint):
            await run_sql(
                "INSERT INTO webhook_attempts (event_id, attempt_no, started_at, duration_ms,"
                f" outcome, status_code) SELECT event_id, {base['attempt_no']}, now(),"
                f" {base['duration_ms']}, {base['outcome']}, {base['status_code']}"
                " FROM webhook_deliveries"
            )

    async def test_an_attempt_needs_a_real_delivery(self, run_sql: RunSql, recorded: Any) -> None:
        with pytest.raises(DBAPIError, match="fk_webhook_attempts_event_id_webhook_deliveries"):
            await run_sql(
                "INSERT INTO webhook_attempts (event_id, attempt_no, started_at, duration_ms,"
                " outcome) VALUES (:e, 1, now(), 1, 'failed')",
                e=uuid4(),
            )


class TestTheMigration:
    async def test_the_downgrade_refuses_to_delete_recorded_attempts(
        self,
        database_url: str,
        run_sql: RunSql,
        pg_service: OrderService,
        pg_uow_factory: UnitOfWorkFactory,
    ) -> None:
        await make_order(pg_service, "ORD-1")
        now = soon()
        async with pg_uow_factory() as uow:
            (taken,) = await uow.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=1)
            attempt = DeliveryAttempt(
                taken.event_id, 1, now, 5, AttemptOutcome.DELIVERED, status_code=200
            )
            assert await uow.webhooks.apply(
                taken, taken.delivered(now=now, status_code=200), attempt
            )
            await uow.commit()

        with pytest.raises(RuntimeError, match="append-only"):
            await asyncio.to_thread(command.downgrade, alembic_config(database_url), "0002")

        assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == "0003"
        assert (await run_sql("SELECT count(*) FROM webhook_attempts"))[0][0] == 1

    async def test_the_downgrade_works_with_no_attempts_and_the_upgrade_restores_it(
        self, database_url: str, run_sql: RunSql, pg_service: OrderService
    ) -> None:
        await make_order(pg_service, "ORD-1")  # a queued delivery, no attempt yet
        config = alembic_config(database_url)

        await asyncio.to_thread(command.downgrade, config, "0002")
        tables = {
            r[0]
            for r in await run_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert not {"webhook_deliveries", "webhook_attempts"} & tables
        assert (await run_sql("SELECT version_num FROM alembic_version"))[0][0] == "0002"

        await asyncio.to_thread(command.upgrade, config, "head")
        tables = {
            r[0]
            for r in await run_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert {"webhook_deliveries", "webhook_attempts"} <= tables
