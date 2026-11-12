"""The audit log's rules for flight events (Rule 5), enforced by the database."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.domain.models import EventSource, FlightStatus, Order, OrderStatus
from app.services.order_service import OrderService
from tests.conftest import DRONE_ID
from tests.integration.conftest import RegisterDrone, RunSql, RunTx
from tests.integration.flights import FlightFlow

pytestmark = pytest.mark.integration

S = FlightStatus


def raw_event(
    order_id: Any,
    flight_id: Any,
    drone_id: Any,
    event_type: str,
    previous: str | None,
    new: str | None,
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO audit_events (event_id, order_id, drone_id, flight_id, event_type,"
        " previous_state, new_state, occurred_at, source) VALUES (:e, :o, :d, :f, :t, :p, :n,"
        " now(), 'ops')",
        {
            "e": uuid4(),
            "o": order_id,
            "d": drone_id,
            "f": flight_id,
            "t": event_type,
            "p": previous,
            "n": new,
        },
    )


@pytest.fixture
async def stored_flight(
    pg_uow_factory: Any, created_order: Order, register_drone: RegisterDrone
) -> Any:
    await register_drone(DRONE_ID)
    return await FlightFlow(pg_uow_factory).flight_in(S.PLANNED, created_order.id, DRONE_ID)


class TestFlightEventsMustBeWellFormed:
    @pytest.mark.parametrize(
        ("event_type", "previous", "new", "with_flight", "with_drone"),
        [
            # state vocabularies must match the event type
            ("flight_created", None, "created", True, True),  # an order state
            ("flight_status_changed", "created", "assigned", True, True),  # order states
            ("order_status_changed", "planned", "cancelled", False, True),  # flight states
            # the NULL trap: IN (...) is NULL for NULL, and a CHECK accepts NULL
            ("flight_status_changed", None, "cancelled", True, True),
            ("flight_status_changed", "planned", None, True, True),
            ("order_status_changed", None, "cancelled", False, True),
            ("order_status_changed", "created", None, False, True),
            # a status change must change something
            ("flight_status_changed", "planned", "planned", True, True),
            # flight events need their flight and their drone
            ("flight_created", None, "planned", False, True),
            ("flight_created", None, "planned", True, False),
            ("flight_status_changed", "planned", "cancelled", False, True),
            ("flight_status_changed", "planned", "cancelled", True, False),
            # order events must not claim a flight
            ("order_created", None, "created", True, True),
            ("order_status_changed", "created", "cancelled", True, True),
            # unknown types
            ("flight_started", None, None, True, True),
        ],
    )
    async def test_malformed_events_are_rejected(
        self,
        created_order: Order,
        stored_flight: Any,
        run_tx: RunTx,
        event_type: str,
        previous: str | None,
        new: str | None,
        with_flight: bool,
        with_drone: bool,
    ) -> None:
        with pytest.raises(DBAPIError, match="violates check constraint"):
            await run_tx(
                raw_event(
                    created_order.id,
                    stored_flight.id if with_flight else None,
                    DRONE_ID if with_drone else None,
                    event_type,
                    previous,
                    new,
                )
            )

    async def test_an_event_for_an_unknown_flight_is_rejected(
        self, created_order: Order, register_drone: RegisterDrone, run_tx: RunTx
    ) -> None:
        await register_drone(DRONE_ID)

        with pytest.raises(DBAPIError, match="violates foreign key constraint"):
            await run_tx(
                raw_event(
                    created_order.id,
                    uuid4(),
                    DRONE_ID,
                    "flight_status_changed",
                    "planned",
                    "cancelled",
                )
            )

    async def test_a_well_formed_flight_event_is_accepted(
        self, created_order: Order, stored_flight: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        # (Inserted on its own: the flight's status is untouched, so no lifecycle check applies.)
        await run_tx(
            raw_event(
                created_order.id,
                stored_flight.id,
                DRONE_ID,
                "flight_status_changed",
                "planned",
                "cancelled",
            )
        )

        count = (
            await run_sql(
                "SELECT count(*) FROM audit_events WHERE flight_id = :f", f=stored_flight.id
            )
        )[0][0]
        assert count == 2


class TestTheOrderOnlyUniquenessDoesNotCatchFlights:
    async def test_a_flight_may_re_enter_a_state_after_a_pause(
        self,
        pg_uow_factory: Any,
        created_order: Order,
        register_drone: RegisterDrone,
        run_sql: RunSql,
    ) -> None:
        await register_drone(DRONE_ID)
        flow = FlightFlow(pg_uow_factory)
        flight = await flow.flight_in(S.PAUSED, created_order.id, DRONE_ID)

        await flow.advance(flight, S.STARTED)

        started = await run_sql(
            "SELECT count(*) FROM audit_events WHERE new_state = 'started' AND flight_id = :f",
            f=flight.id,
        )
        assert started[0][0] == 2

    async def test_a_cancelled_flight_and_a_cancelled_order_coexist(
        self,
        pg_uow_factory: Any,
        pg_service: OrderService,
        created_order: Order,
        register_drone: RegisterDrone,
        run_sql: RunSql,
    ) -> None:
        """Both vocabularies contain "cancelled"; the one-event-per-state rule is for orders."""
        await register_drone(DRONE_ID)
        await FlightFlow(pg_uow_factory).flight_in(S.CANCELLED, created_order.id, DRONE_ID)

        await pg_service.transition(created_order.id, OrderStatus.CANCELLED, source=EventSource.API)

        rows = await run_sql(
            "SELECT event_type, new_state FROM audit_events WHERE new_state = 'cancelled'"
        )
        assert sorted(r[0] for r in rows) == ["flight_status_changed", "order_status_changed"]

    async def test_an_order_still_cannot_enter_the_same_state_twice(
        self, created_order: Order, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="uq_audit_events_order_new_state"):
            await run_tx(
                (
                    "INSERT INTO audit_events (event_id, order_id, event_type, previous_state,"
                    " new_state, occurred_at, source) VALUES (:e, :o, 'order_created', NULL,"
                    " 'created', now(), 'ops')",
                    {"e": uuid4(), "o": created_order.id},
                )
            )


class TestTheAuditTrailReadsBackCorrectly:
    async def test_flight_events_come_back_with_flight_states_and_order_events_with_order_states(
        self,
        pg_uow_factory: Any,
        pg_service: OrderService,
        created_order: Order,
        register_drone: RegisterDrone,
    ) -> None:
        await register_drone(DRONE_ID)
        await FlightFlow(pg_uow_factory).flight_in(S.CANCELLED, created_order.id, DRONE_ID)
        await pg_service.transition(created_order.id, OrderStatus.CANCELLED, source=EventSource.API)

        trail = await pg_service.get_audit_trail(created_order.id)

        order_events = [e for e in trail if e.event_type.value.startswith("order")]
        flight_events = [e for e in trail if e.event_type.value.startswith("flight")]
        assert all(isinstance(e.new_state, OrderStatus) for e in order_events)
        assert all(isinstance(e.new_state, FlightStatus) for e in flight_events)
        # the shared string must come back as the right type for each vocabulary
        assert order_events[-1].new_state is OrderStatus.CANCELLED
        assert flight_events[-1].new_state is S.CANCELLED
        assert [e.event_type.value for e in trail] == [
            "order_created",
            "flight_created",
            "flight_status_changed",
            "order_status_changed",
        ]
