"""Concurrent transitions on one order are serialised by the row lock."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.domain.exceptions import DomainError, InvalidTransitionError, TerminalStateError
from app.domain.models import EventSource, Location, Order, OrderStatus, Package
from app.services.order_service import OrderService
from tests.integration.conftest import RunSql

pytestmark = pytest.mark.integration

S = OrderStatus


async def test_only_one_of_many_concurrent_assignments_wins(
    pg_service: OrderService, created_order: Order
) -> None:
    drones = [UUID(int=i) for i in range(1, 9)]

    outcomes = await asyncio.gather(
        *(
            pg_service.transition(
                created_order.id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=drone
            )
            for drone in drones
        ),
        return_exceptions=True,
    )

    winners = [o for o in outcomes if isinstance(o, Order)]
    losers = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 7
    # Losers saw the new status under the lock and were rejected by the state machine,
    # not by a database error.
    assert all(isinstance(e, InvalidTransitionError) for e in losers)

    final = await pg_service.get_order(created_order.id)
    assert final.status is S.ASSIGNED
    assert final.drone_id == winners[0].drone_id
    trail = await pg_service.get_audit_trail(created_order.id)
    assert [e.new_state for e in trail] == [S.CREATED, S.ASSIGNED]
    assert trail[-1].drone_id == winners[0].drone_id


async def test_concurrent_cancellations_cancel_once(
    pg_service: OrderService, created_order: Order, run_sql: RunSql
) -> None:
    outcomes = await asyncio.gather(
        *(
            pg_service.transition(created_order.id, S.CANCELLED, source=EventSource.API)
            for _ in range(6)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(o, Order) for o in outcomes) == 1
    assert all(isinstance(o, TerminalStateError) for o in outcomes if not isinstance(o, Order))
    cancelled_events = await run_sql(
        "SELECT count(*) FROM audit_events WHERE new_state = 'cancelled'"
    )
    assert cancelled_events[0][0] == 1


async def test_racing_assign_and_cancel_always_leave_a_consistent_history(
    pg_service: OrderService, created_order: Order
) -> None:
    """Either order is legal (assign then cancel, or cancel then a rejected assign).

    Whatever interleaving happens, the stored status must equal the last audit event
    and every audit event must start where the previous one ended.
    """
    assign = pg_service.transition(
        created_order.id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=UUID(int=5)
    )
    cancel = pg_service.transition(created_order.id, S.CANCELLED, source=EventSource.API)

    outcomes = await asyncio.gather(assign, cancel, return_exceptions=True)

    assert not [
        o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, DomainError)
    ]
    final = await pg_service.get_order(created_order.id)
    assert final.status is S.CANCELLED, "cancel is legal from both created and assigned"
    trail = await pg_service.get_audit_trail(created_order.id)
    assert trail[-1].new_state is final.status
    assert [e.previous_state for e in trail[1:]] == [e.new_state for e in trail[:-1]]
    assert len({e.new_state for e in trail}) == len(trail)


async def test_transitions_on_different_orders_do_not_block_each_other(
    pg_service: OrderService,
    created_order: Order,
    pickup: Location,
    dropoff: Location,
    package: Package,
) -> None:
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

    results = await asyncio.wait_for(
        asyncio.gather(
            pg_service.transition(created_order.id, S.CANCELLED, source=EventSource.API),
            pg_service.transition(other.id, S.CANCELLED, source=EventSource.API),
        ),
        timeout=10,
    )

    assert [r.status for r in results] == [S.CANCELLED, S.CANCELLED]
