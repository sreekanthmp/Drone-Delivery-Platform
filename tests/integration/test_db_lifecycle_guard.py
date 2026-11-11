"""Rule 4 enforced by the database: raw SQL cannot bypass the state machine.

The ``orders_enforce_lifecycle`` constraint trigger is deferred, so violations
surface when the transaction commits. These tests go around the application on
purpose, with hand-written SQL, to prove the database refuses what the Python
layers would never attempt.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.domain.models import EventSource, Location, Order, OrderStatus, Package
from app.services.order_service import OrderService
from tests.integration.conftest import RunSql, RunTx

pytestmark = pytest.mark.integration

S = OrderStatus
DRONE = UUID(int=0xD0)

Statement = tuple[str, dict[str, Any]]


def forged_audit(
    order_id: UUID, previous: str, new: str, drone_id: UUID | None = None
) -> Statement:
    """A hand-written ``order_status_changed`` audit row (what the state machine would write)."""
    return (
        "INSERT INTO audit_events (event_id, order_id, drone_id, event_type, previous_state,"
        " new_state, occurred_at, source) VALUES (:event_id, :order_id, :drone_id,"
        " 'order_status_changed', :previous, :new, now(), 'ops')",
        {
            "event_id": uuid4(),
            "order_id": order_id,
            "drone_id": drone_id,
            "previous": previous,
            "new": new,
        },
    )


def raw_status_update(order_id: UUID, new: str) -> Statement:
    return ("UPDATE orders SET status = :new WHERE id = :id", {"id": order_id, "new": new})


def raw_order(order_id: UUID, ext: str, status: str) -> Statement:
    return (
        "INSERT INTO orders (id, client_id, external_order_id, pickup, dropoff,"
        " package_weight_kg, status, created_at, updated_at) VALUES (:id, 'raw', :ext,"
        " ST_GeogFromText('POINT(1 1)'), ST_GeogFromText('POINT(2 2)'), 1.0, :status,"
        " now(), now())",
        {"id": order_id, "ext": ext, "status": status},
    )


def created_audit(order_id: UUID) -> Statement:
    return (
        "INSERT INTO audit_events (event_id, order_id, event_type, previous_state, new_state,"
        " occurred_at, source) VALUES (:event_id, :order_id, 'order_created', NULL, 'created',"
        " now(), 'ops')",
        {"event_id": uuid4(), "order_id": order_id},
    )


async def _status(run_sql: RunSql, order_id: UUID) -> str:
    return (await run_sql("SELECT status FROM orders WHERE id = :id", id=order_id))[0][0]


async def _audit_count(run_sql: RunSql) -> int:
    return (await run_sql("SELECT count(*) FROM audit_events"))[0][0]


class TestStatusChangesNeedTheStateMachinesBlessing:
    async def test_a_raw_status_update_without_an_audit_event_fails(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="without an audit event"):
            await run_tx(raw_status_update(created_order.id, "assigned"))

        assert await _status(run_sql, created_order.id) == "created"

    async def test_another_orders_audit_event_does_not_count(
        self,
        pg_service: OrderService,
        created_order: Order,
        pickup: Location,
        dropoff: Location,
        package: Package,
        run_sql: RunSql,
        run_tx: RunTx,
    ) -> None:
        """Order A legitimately goes created -> assigned, leaving a matching audit row.

        A raw update moving order B through the same edge must still be refused: the audit
        event has to belong to *that* order.
        """
        other = (
            await pg_service.create_order(
                client_id="mock",
                external_order_id="OTHER-1",
                pickup=pickup,
                dropoff=dropoff,
                package=package,
                source=EventSource.API,
            )
        ).order
        await pg_service.transition(
            created_order.id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=DRONE
        )

        with pytest.raises(DBAPIError, match="without an audit event"):
            await run_tx(raw_status_update(other.id, "assigned"))

        assert await _status(run_sql, other.id) == "created"
        assert await _status(run_sql, created_order.id) == "assigned"

    async def test_an_invalid_edge_fails_even_with_a_forged_audit_event(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="cannot move from created to delivered"):
            await run_tx(
                forged_audit(created_order.id, "created", "delivered", DRONE),
                raw_status_update(created_order.id, "delivered"),
            )

        assert await _status(run_sql, created_order.id) == "created"
        assert await _audit_count(run_sql) == 1, "the forged audit row rolled back with it"

    async def test_a_valid_edge_with_its_audit_event_in_the_same_transaction_succeeds(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        await run_tx(
            forged_audit(created_order.id, "created", "cancelled"),
            raw_status_update(created_order.id, "cancelled"),
        )

        assert await _status(run_sql, created_order.id) == "cancelled"

    async def test_the_audit_event_may_be_inserted_after_the_update(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """The check is deferred to commit, so statement order inside a transaction is free."""
        await run_tx(
            raw_status_update(created_order.id, "cancelled"),
            forged_audit(created_order.id, "created", "cancelled"),
        )

        assert await _status(run_sql, created_order.id) == "cancelled"

    async def test_several_steps_in_one_transaction_are_each_checked(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        await run_tx(
            forged_audit(created_order.id, "created", "assigned", DRONE),
            raw_status_update(created_order.id, "assigned"),
            forged_audit(created_order.id, "assigned", "picked_up", DRONE),
            raw_status_update(created_order.id, "picked_up"),
        )

        assert await _status(run_sql, created_order.id) == "picked_up"

    async def test_skipping_a_step_is_rejected_even_if_the_hop_has_an_audit_row(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="cannot move from created to picked_up"):
            await run_tx(
                forged_audit(created_order.id, "created", "picked_up", DRONE),
                raw_status_update(created_order.id, "picked_up"),
            )

        assert await _status(run_sql, created_order.id) == "created"

    @pytest.mark.parametrize(
        ("revive_to", "rejected_by"),
        [
            # Already entered 'created' once, so the audit UNIQUE(order_id, new_state) index
            # refuses the forged row before the lifecycle trigger even runs.
            ("created", "uq_audit_events_order_new_state"),
            # Never entered 'assigned', so the forged row is accepted and the lifecycle
            # trigger's edge check is what refuses the move.
            ("assigned", "cannot move from cancelled to assigned"),
        ],
    )
    async def test_a_terminal_order_cannot_be_revived(
        self,
        pg_service: OrderService,
        created_order: Order,
        run_sql: RunSql,
        run_tx: RunTx,
        revive_to: str,
        rejected_by: str,
    ) -> None:
        await pg_service.transition(created_order.id, S.CANCELLED, source=EventSource.API)

        with pytest.raises(DBAPIError, match=rejected_by):
            await run_tx(
                forged_audit(created_order.id, "cancelled", revive_to, DRONE),
                raw_status_update(created_order.id, revive_to),
            )

        assert await _status(run_sql, created_order.id) == "cancelled"
        assert await _audit_count(run_sql) == 2, "nothing from the failed attempt was kept"

    async def test_updating_other_columns_needs_no_audit_event(
        self, created_order: Order, run_sql: RunSql
    ) -> None:
        await run_sql(
            "UPDATE orders SET package_description = 'fragile', updated_at = now() WHERE id = :id",
            id=created_order.id,
        )

        rows = await run_sql(
            "SELECT package_description FROM orders WHERE id = :id", id=created_order.id
        )
        assert rows[0][0] == "fragile"
        assert await _status(run_sql, created_order.id) == "created"


class TestNewOrdersNeedTheirCreationEvent:
    async def test_an_order_without_an_audit_event_cannot_be_inserted(
        self, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="no order_created audit event"):
            await run_tx(raw_order(uuid4(), "e1", "created"))

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 0

    @pytest.mark.parametrize("status", ["assigned", "in_flight", "delivered", "cancelled"])
    async def test_an_order_cannot_be_inserted_past_created(
        self, run_sql: RunSql, run_tx: RunTx, status: str
    ) -> None:
        order_id = uuid4()

        with pytest.raises(DBAPIError, match="must be inserted in status created"):
            await run_tx(raw_order(order_id, "e2", status), created_audit(order_id))

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 0

    async def test_an_order_with_its_creation_event_can_be_inserted(
        self, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        order_id = uuid4()

        await run_tx(raw_order(order_id, "e3", "created"), created_audit(order_id))

        assert await _status(run_sql, order_id) == "created"

    async def test_another_orders_creation_event_does_not_count(
        self, created_order: Order, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """``created_order`` has its creation event; a second order without one is still refused."""
        with pytest.raises(DBAPIError, match="no order_created audit event"):
            await run_tx(raw_order(uuid4(), "e4", "created"))

        assert (await run_sql("SELECT count(*) FROM orders"))[0][0] == 1
