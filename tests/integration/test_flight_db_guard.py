"""Rule 2 enforced by the database: raw SQL cannot start a flight without a valid permission.

The ``flights_enforce_lifecycle`` constraint trigger is deferred, so violations surface when
the transaction commits. These tests go around the application with hand-written SQL, on
flights that were stored the proper way, to prove the database refuses what the Python
layers would never attempt.

What the database can and cannot see: it enforces the *data* conditions of the gate (a
stored permission, an acknowledged mission, a permission that is in force and long enough)
and that every change is an allowed edge with an audit row from the same transaction. It
cannot see the provider re-check, so a raw start that satisfies all of those conditions is
accepted; ``test_a_start_that_meets_every_data_condition_is_accepted`` documents that limit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.domain.models import FlightStatus, Order
from app.services.order_service import OrderService
from tests.conftest import DRONE_ID, build_mission
from tests.integration.conftest import RegisterDrone, RunSql, RunTx
from tests.integration.flights import FlightFlow

pytestmark = pytest.mark.integration

S = FlightStatus
OTHER_DRONE = UUID(int=0xD1)
Statement = tuple[str, dict[str, Any]]


def forged_flight_audit(
    flight_id: UUID, order_id: UUID, drone_id: UUID, previous: str, new: str
) -> Statement:
    """A hand-written ``flight_status_changed`` audit row (what the state machine would write)."""
    return (
        "INSERT INTO audit_events (event_id, order_id, drone_id, flight_id, event_type,"
        " previous_state, new_state, occurred_at, source) VALUES (:event_id, :order_id,"
        " :drone_id, :flight_id, 'flight_status_changed', :previous, :new, now(), 'ops')",
        {
            "event_id": uuid4(),
            "order_id": order_id,
            "drone_id": drone_id,
            "flight_id": flight_id,
            "previous": previous,
            "new": new,
        },
    )


def forged_created_audit(flight_id: UUID, order_id: UUID, drone_id: UUID) -> Statement:
    return (
        "INSERT INTO audit_events (event_id, order_id, drone_id, flight_id, event_type,"
        " previous_state, new_state, occurred_at, source) VALUES (:event_id, :order_id,"
        " :drone_id, :flight_id, 'flight_created', NULL, 'planned', now(), 'ops')",
        {
            "event_id": uuid4(),
            "order_id": order_id,
            "drone_id": drone_id,
            "flight_id": flight_id,
        },
    )


def raw_status(flight_id: UUID, new: str, extra_sql: str = "") -> Statement:
    return (
        f"UPDATE flights SET status = :new{extra_sql} WHERE id = :id",
        {"id": flight_id, "new": new},
    )


def raw_flight(
    flight_id: UUID, order_id: UUID, drone_id: UUID, status: str = "planned", **columns: Any
) -> Statement:
    mission = build_mission()
    values: dict[str, Any] = {
        "id": flight_id,
        "order_id": order_id,
        "drone_id": drone_id,
        "status": status,
        "mission": json.dumps(mission.as_dict()),
        "checksum": mission.checksum,
        "duration": mission.estimated_duration_s,
    }
    extra_columns = "".join(f", {name}" for name in columns)
    extra_values = "".join(f", :{name}" for name in columns)
    return (
        "INSERT INTO flights (id, order_id, drone_id, status, mission, mission_checksum,"
        f" mission_estimated_duration_s, created_at, updated_at{extra_columns})"
        " VALUES (:id, :order_id, :drone_id, :status, CAST(:mission AS jsonb), :checksum,"
        f" :duration, now(), now(){extra_values})",
        {**values, **columns},
    )


async def _status(run_sql: RunSql, flight_id: UUID) -> str:
    return (await run_sql("SELECT status FROM flights WHERE id = :id", id=flight_id))[0][0]


@pytest.fixture
def flow(pg_uow_factory: Any) -> FlightFlow:
    return FlightFlow(pg_uow_factory)


@pytest.fixture
async def drones(register_drone: RegisterDrone) -> tuple[UUID, UUID]:
    return await register_drone(DRONE_ID), await register_drone(OTHER_DRONE)


@pytest.fixture
async def second_order(pg_service: OrderService, pickup: Any, dropoff: Any, package: Any) -> Order:
    return (
        await pg_service.create_order(
            client_id="mock",
            external_order_id="SECOND",
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            source=__import__("app.domain.models", fromlist=["EventSource"]).EventSource.API,
        )
    ).order


class TestEveryChangeNeedsAnEdgeAndAnAuditEvent:
    async def test_a_raw_status_update_without_an_audit_event_fails(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="without an audit event"):
            await run_tx(raw_status(flight.id, "cancelled", ", failure_reason = 'x'"))

        assert await _status(run_sql, flight.id) == "planned"

    async def test_an_invalid_edge_fails_even_with_a_forged_audit_event(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        # mission_uploaded -> completed skips the whole flight. started_at is supplied so the
        # row-level CHECKs pass and the lifecycle trigger's edge check is what refuses it.
        with pytest.raises(DBAPIError, match="cannot move from mission_uploaded to completed"):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "mission_uploaded", "completed"
                ),
                raw_status(flight.id, "completed", ", started_at = now()"),
            )

        assert await _status(run_sql, flight.id) == "mission_uploaded"
        count = (
            await run_sql("SELECT count(*) FROM audit_events WHERE flight_id = :f", f=flight.id)
        )[0][0]
        assert count == 3, "the forged audit row rolled back with it"

    async def test_skipping_the_upload_is_refused_by_the_database_too(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """permission_granted -> started with the ack forged in: refused (write-once and edge)."""
        flight = await flow.flight_in(S.PERMISSION_GRANTED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match=r"write-once|cannot move"):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "permission_granted", "started"
                ),
                raw_status(
                    flight.id,
                    "started",
                    ", started_at = now(), acknowledged_checksum = mission_checksum",
                ),
            )

        assert await _status(run_sql, flight.id) == "permission_granted"

    async def test_a_valid_edge_with_its_audit_event_in_the_same_transaction_succeeds(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)

        await run_tx(
            forged_flight_audit(flight.id, created_order.id, DRONE_ID, "planned", "cancelled"),
            raw_status(flight.id, "cancelled", ", failure_reason = 'operator'"),
        )

        assert await _status(run_sql, flight.id) == "cancelled"

    async def test_an_audit_event_from_an_earlier_transaction_does_not_count(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """Pause and resume repeat edges, so an old started->paused event must not be reusable."""
        flight = await flow.flight_in(S.PAUSED, created_order.id, DRONE_ID)
        flight = await flow.advance(flight, S.STARTED)  # started again; the old pause event exists

        with pytest.raises(DBAPIError, match="without an audit event from this transaction"):
            await run_tx(raw_status(flight.id, "paused"))

        assert await _status(run_sql, flight.id) == "started"

    async def test_a_finished_flight_cannot_be_revived(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.CANCELLED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="cannot move from cancelled to planned"):
            await run_tx(
                forged_flight_audit(flight.id, created_order.id, DRONE_ID, "cancelled", "planned"),
                raw_status(flight.id, "planned", ", failure_reason = NULL"),
            )

        assert await _status(run_sql, flight.id) == "cancelled"

    async def test_updating_columns_that_may_change_needs_no_audit_event(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql
    ) -> None:
        flight = await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)

        await run_sql("UPDATE flights SET updated_at = now() WHERE id = :id", id=flight.id)

        assert await _status(run_sql, flight.id) == "planned"


class TestNewFlightsNeedTheirCreationEvent:
    async def test_a_flight_without_a_creation_event_cannot_be_inserted(
        self, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        with pytest.raises(DBAPIError, match="no flight_created audit event"):
            await run_tx(raw_flight(uuid4(), created_order.id, DRONE_ID))

        assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 0

    async def test_a_flight_cannot_be_inserted_past_planned(
        self, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight_id = uuid4()

        with pytest.raises(DBAPIError, match="must be inserted in status planned"):
            await run_tx(
                raw_flight(flight_id, created_order.id, DRONE_ID, "cancelled", failure_reason="x"),
                forged_created_audit(flight_id, created_order.id, DRONE_ID),
            )

        assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 0

    async def test_a_planned_flight_with_its_creation_event_can_be_inserted(
        self, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight_id = uuid4()

        # The audit row references the flight, so the flight row goes first; the check is deferred.
        await run_tx(
            raw_flight(flight_id, created_order.id, DRONE_ID),
            forged_created_audit(flight_id, created_order.id, DRONE_ID),
        )

        assert await _status(run_sql, flight_id) == "planned"


class TestTheStartGate:
    """Rule 2 in the database: entering 'started' requires a permission that is in force."""

    async def test_a_start_that_meets_every_data_condition_is_accepted(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """Documents the limit: the database cannot see the provider re-check."""
        flight = await flow.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        await run_tx(
            forged_flight_audit(
                flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
            ),
            raw_status(flight.id, "started", ", started_at = now()"),
        )

        assert await _status(run_sql, flight.id) == "started"

    async def test_an_expired_permission_cannot_start_a_flight(
        self, pg_uow_factory: Any, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        in_the_past = FlightFlow(
            pg_uow_factory, clock=lambda: datetime.now(UTC) - timedelta(hours=1)
        )
        flight = await in_the_past.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="its permission has expired"):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
                ),
                raw_status(flight.id, "started", ", started_at = now()"),
            )

        assert await _status(run_sql, flight.id) == "mission_uploaded"

    async def test_a_permission_that_ends_before_the_mission_would_finish_cannot_start_it(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        two_hour_mission = build_mission(estimated_duration_s=7200.0)  # permission lasts 15 min
        flight = await flow.flight_in(
            S.MISSION_UPLOADED, created_order.id, DRONE_ID, two_hour_mission
        )

        with pytest.raises(DBAPIError, match="ends before the mission would finish"):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
                ),
                raw_status(flight.id, "started", ", started_at = now()"),
            )

        assert await _status(run_sql, flight.id) == "mission_uploaded"

    @pytest.mark.parametrize(("validity_minutes", "starts"), [(5, False), (20, True)])
    async def test_the_window_must_cover_a_ten_minute_mission(
        self,
        pg_uow_factory: Any,
        created_order: Order,
        drones: Any,
        run_sql: RunSql,
        run_tx: RunTx,
        validity_minutes: float,
        starts: bool,
    ) -> None:
        flight = await FlightFlow(pg_uow_factory, validity_minutes=validity_minutes).flight_in(
            S.MISSION_UPLOADED, created_order.id, DRONE_ID
        )
        attempt = run_tx(
            forged_flight_audit(
                flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
            ),
            raw_status(flight.id, "started", ", started_at = now()"),
        )

        if starts:
            await attempt
            assert await _status(run_sql, flight.id) == "started"
        else:
            with pytest.raises(DBAPIError, match="before the mission would finish"):
                await attempt

    async def test_resuming_after_the_permission_expired_is_refused_too(
        self, pg_uow_factory: Any, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        """A flight that was legitimately started, then paused while its permission ran out.

        Real time has to pass for this (the database judges validity by its own clock), so
        the permission is short-lived and the mission is one second long.
        """
        import asyncio

        flow = FlightFlow(pg_uow_factory, validity_minutes=4 / 60)  # four seconds
        flight = await flow.flight_in(
            S.PAUSED, created_order.id, DRONE_ID, build_mission(estimated_duration_s=1.0)
        )
        assert flight.permission is not None
        wait = (flight.permission.valid_until - datetime.now(UTC)).total_seconds() + 0.3
        assert wait < 5, "setup was too slow for this test to mean anything"
        await asyncio.sleep(max(wait, 0))

        with pytest.raises(DBAPIError, match="its permission has expired"):
            await run_tx(
                forged_flight_audit(flight.id, created_order.id, DRONE_ID, "paused", "started"),
                raw_status(flight.id, "started"),
            )

        assert await _status(run_sql, flight.id) == "paused"

    async def test_a_start_cannot_erase_the_permission_to_get_past_the_gate(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
                ),
                raw_status(
                    flight.id,
                    "started",
                    ", started_at = now(), permission_id = NULL, permission_issuer = NULL,"
                    " permission_issued_at = NULL, permission_valid_from = NULL,"
                    " permission_valid_until = NULL",
                ),
            )

        assert await _status(run_sql, flight.id) == "mission_uploaded"

    async def test_a_start_cannot_extend_the_permission_to_get_past_the_gate(
        self, pg_uow_factory: Any, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        in_the_past = FlightFlow(
            pg_uow_factory, clock=lambda: datetime.now(UTC) - timedelta(hours=1)
        )
        flight = await in_the_past.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="write-once"):
            await run_tx(
                forged_flight_audit(
                    flight.id, created_order.id, DRONE_ID, "mission_uploaded", "started"
                ),
                raw_status(
                    flight.id,
                    "started",
                    ", started_at = now(),"
                    " permission_valid_until = permission_valid_until + interval '1 day'",
                ),
            )

        assert await _status(run_sql, flight.id) == "mission_uploaded"


class TestWriteOnceAndImmutableColumns:
    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("permission_valid_until = permission_valid_until + interval '1 day'", "write-once"),
            ("permission_id = 'SOMEONE-ELSES'", "write-once"),
            ("permission_issuer = 'me'", "write-once"),
            ("mission = '{}'::jsonb", "immutable"),
            ("mission_estimated_duration_s = 1", "immutable"),
            ("order_id = :other_order", "immutable"),
            ("drone_id = :other_drone", "immutable"),
        ],
    )
    async def test_a_granted_and_uploaded_flight_keeps_what_it_was_planned_with(
        self,
        flow: FlightFlow,
        created_order: Order,
        second_order: Order,
        drones: Any,
        run_sql: RunSql,
        run_tx: RunTx,
        sql: str,
        message: str,
    ) -> None:
        flight = await flow.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)
        before = [
            tuple(r) for r in await run_sql("SELECT * FROM flights WHERE id = :id", id=flight.id)
        ]

        with pytest.raises(DBAPIError, match=message):
            await run_tx(
                (
                    f"UPDATE flights SET {sql} WHERE id = :id",
                    {"id": flight.id, "other_order": second_order.id, "other_drone": OTHER_DRONE},
                )
            )

        after = [
            tuple(r) for r in await run_sql("SELECT * FROM flights WHERE id = :id", id=flight.id)
        ]
        assert after == before

    @pytest.mark.parametrize(
        "sql",
        [
            "mission_checksum = 'x'",  # also breaks the ack-matches-mission CHECK
            "acknowledged_checksum = NULL",  # ack is required once uploaded
            "acknowledged_checksum = 'x'",  # must match the mission
        ],
    )
    async def test_the_mission_ack_cannot_be_tampered_with(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_tx: RunTx, sql: str
    ) -> None:
        flight = await flow.flight_in(S.MISSION_UPLOADED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError):
            await run_tx((f"UPDATE flights SET {sql} WHERE id = :id", {"id": flight.id}))

    async def test_started_at_is_write_once(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.STARTED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="started_at is write-once"):
            await run_tx(
                (
                    "UPDATE flights SET started_at = started_at + interval '1 hour' WHERE id = :id",
                    {"id": flight.id},
                )
            )

    async def test_a_denial_is_write_once(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.PERMISSION_DENIED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="denial fields are write-once"):
            await run_tx(
                (
                    "UPDATE flights SET denial_reason = 'rewritten' WHERE id = :id",
                    {"id": flight.id},
                )
            )

    async def test_a_permission_cannot_be_attached_without_going_through_the_grant(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_tx: RunTx
    ) -> None:
        flight = await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError):
            await run_tx(
                (
                    "UPDATE flights SET permission_id = 'P', permission_issuer = 'i',"
                    " permission_issued_at = now(), permission_valid_from = now(),"
                    " permission_valid_until = now() + interval '1 day' WHERE id = :id",
                    {"id": flight.id},
                )
            )


class TestFieldRulesMirrorTheDomain:
    @pytest.mark.parametrize(
        ("status", "columns", "constraint"),
        [
            ("teleported", {}, "status_valid"),
            ("planned", {"mission_estimated_duration_s": 0}, "mission_duration_positive"),
            # 'cancelled' makes a permission optional, so only the all-or-nothing rule applies.
            (
                "cancelled",
                {"failure_reason": "x", "permission_id": "P"},
                "permission_fields_together",
            ),
            ("planned", {"permission_id": "P"}, "no_permission_before_grant"),
            ("planned", {"denial_code": "rejected"}, "denial_fields_together"),
            (
                "planned",
                {"denial_code": "rejected", "denial_reason": "no"},
                "denial_matches_status",
            ),
            ("planned", {"acknowledged_checksum": "0" * 64}, "ack_forbidden_before_upload"),
            (
                "planned",
                {"started_at": datetime(2026, 1, 1, tzinfo=UTC)},
                "started_at_matches_status",
            ),
            ("planned", {"failure_reason": "why"}, "reason_matches_status"),
            ("cancelled", {}, "reason_matches_status"),  # a cancelled flight needs its reason
        ],
    )
    async def test_a_flight_with_inconsistent_fields_is_refused(
        self,
        created_order: Order,
        drones: Any,
        run_tx: RunTx,
        status: str,
        columns: dict[str, Any],
        constraint: str,
    ) -> None:
        flight_id = uuid4()
        columns = dict(columns)
        duration = columns.pop("mission_estimated_duration_s", None)
        statement, params = raw_flight(flight_id, created_order.id, DRONE_ID, status, **columns)
        if duration is not None:
            params["duration"] = duration

        with pytest.raises(DBAPIError, match=constraint):
            await run_tx(
                (statement, params), forged_created_audit(flight_id, created_order.id, DRONE_ID)
            )


class TestRelationsAndUniqueness:
    async def test_a_drone_cannot_have_two_live_flights(
        self,
        flow: FlightFlow,
        created_order: Order,
        second_order: Order,
        drones: Any,
        run_tx: RunTx,
    ) -> None:
        await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)
        rival = uuid4()

        with pytest.raises(DBAPIError, match="uq_flights_live_per_drone"):
            await run_tx(
                raw_flight(rival, second_order.id, DRONE_ID),
                forged_created_audit(rival, second_order.id, DRONE_ID),
            )

    async def test_an_order_cannot_have_two_live_flights(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_tx: RunTx
    ) -> None:
        await flow.flight_in(S.PLANNED, created_order.id, DRONE_ID)
        rival = uuid4()

        with pytest.raises(DBAPIError, match="uq_flights_live_per_order"):
            await run_tx(
                raw_flight(rival, created_order.id, OTHER_DRONE),
                forged_created_audit(rival, created_order.id, OTHER_DRONE),
            )

    async def test_a_finished_flight_does_not_block_a_new_one(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, run_tx: RunTx
    ) -> None:
        await flow.flight_in(S.CANCELLED, created_order.id, DRONE_ID)
        fresh = uuid4()

        await run_tx(
            raw_flight(fresh, created_order.id, DRONE_ID),
            forged_created_audit(fresh, created_order.id, DRONE_ID),
        )

        assert await _status(run_sql, fresh) == "planned"

    @pytest.mark.parametrize(
        "statement",
        ["DELETE FROM flights", "DELETE FROM drones", "DELETE FROM orders"],
    )
    async def test_nothing_with_flights_or_audit_history_can_be_deleted(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql, statement: str
    ) -> None:
        await flow.flight_in(S.CANCELLED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="violates foreign key constraint"):
            await run_sql(statement)

        assert (await run_sql("SELECT count(*) FROM flights"))[0][0] == 1

    async def test_truncating_flights_cannot_erase_the_audit_trail(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql
    ) -> None:
        await flow.flight_in(S.CANCELLED, created_order.id, DRONE_ID)
        before = (await run_sql("SELECT count(*) FROM audit_events"))[0][0]

        with pytest.raises(DBAPIError):
            await run_sql("TRUNCATE flights CASCADE")

        assert (await run_sql("SELECT count(*) FROM audit_events"))[0][0] == before

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO flight_transitions VALUES ('planned', 'completed')",
            "UPDATE flight_transitions SET to_status = 'completed' WHERE from_status = 'planned'",
            "DELETE FROM flight_transitions",
            "TRUNCATE flight_transitions",
        ],
    )
    async def test_the_edge_table_is_immutable(self, run_sql: RunSql, statement: str) -> None:
        with pytest.raises(DBAPIError, match="not allowed"):
            await run_sql(statement)

        assert (await run_sql("SELECT count(*) FROM flight_transitions"))[0][0] == 13

    async def test_flight_audit_events_are_append_only_too(
        self, flow: FlightFlow, created_order: Order, drones: Any, run_sql: RunSql
    ) -> None:
        flight = await flow.flight_in(S.PERMISSION_GRANTED, created_order.id, DRONE_ID)

        with pytest.raises(DBAPIError, match="UPDATE on table audit_events is not allowed"):
            await run_sql(
                "UPDATE audit_events SET source = 'api' WHERE flight_id = :f", f=flight.id
            )
        with pytest.raises(DBAPIError, match="DELETE on table audit_events is not allowed"):
            await run_sql("DELETE FROM audit_events WHERE flight_id = :f", f=flight.id)

    async def test_an_order_cannot_be_assigned_to_an_unregistered_drone(
        self, pg_service: OrderService, created_order: Order, run_sql: RunSql
    ) -> None:
        from app.domain.models import EventSource, OrderStatus

        with pytest.raises(DBAPIError, match="violates foreign key constraint"):
            await pg_service.transition(
                created_order.id,
                OrderStatus.ASSIGNED,
                source=EventSource.DISPATCH,
                drone_id=UUID(int=0xBAD),
            )

        status = (await run_sql("SELECT status FROM orders WHERE id = :id", id=created_order.id))[
            0
        ][0]
        assert status == "created"
