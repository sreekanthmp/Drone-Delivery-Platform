"""Drones, flights, and the flight lifecycle guard (Rule 2).

Hand-written, like 0001. What it adds:

* ``drones``: the fleet registry. ``orders.drone_id`` now references it (RESTRICT).
* ``flights``: one drone flying one mission for one order, with its own lifecycle.
  - At most one *live* flight per drone and per order (partial unique indexes), so a drone
    cannot be double-booked at the database level.
  - ``CHECK`` constraints mirroring the domain's per-status field rules.
  - ``flights_enforce_lifecycle`` (deferred constraint trigger): every new flight is
    ``planned`` with a ``flight_created`` audit row from the same transaction; every status
    change is an edge in ``flight_transitions`` with a matching audit row from the same
    transaction; the mission and permission columns are write-once; and **entering
    ``started`` requires a stored permission, an acknowledged mission checksum, and a
    permission that is still in force and covers the mission's duration** (Rule 2). A raw
    ``UPDATE flights SET status = 'started'`` that skips the gate therefore fails.
* ``flight_transitions``: the allowed flight edges, seeded here and immutable.
* ``audit_events``: a ``flight_id`` column, and the CHECK constraints rewritten so flight
  events (a different state vocabulary) are accepted alongside order events. The append-only
  triggers from 0001 are untouched.

Like the orders guard, these protect against mistakes and raw-SQL bypass; a database owner
or superuser can still drop triggers. The trigger compares the permission's end with the
database clock, so keep application and database clocks in sync.

Downgrade refuses to run if flight audit events exist: audit data is append-only, so a
migration will not delete it.

Each ``op.execute`` is a single statement (asyncpg cannot prepare multi-statement strings).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Frozen snapshots. Unit tests fail if any of these drifts from the domain or the models.
FLIGHT_TRANSITIONS: list[tuple[str, str]] = [
    ("planned", "permission_granted"),
    ("planned", "permission_denied"),
    ("planned", "cancelled"),
    ("permission_granted", "mission_uploaded"),
    ("permission_granted", "upload_failed"),
    ("permission_granted", "cancelled"),
    ("mission_uploaded", "started"),
    ("mission_uploaded", "cancelled"),
    ("started", "paused"),
    ("started", "completed"),
    ("started", "aborted"),
    ("paused", "started"),
    ("paused", "aborted"),
]

ORDER_STATUSES = (
    "created",
    "assigned",
    "picked_up",
    "in_flight",
    "arriving",
    "delivered",
    "failed",
    "cancelled",
    "returned",
)
FLIGHT_STATUSES = (
    "planned",
    "permission_granted",
    "permission_denied",
    "mission_uploaded",
    "upload_failed",
    "started",
    "paused",
    "completed",
    "aborted",
    "cancelled",
)
DRONE_STATUSES = ("available", "assigned", "on_mission", "returning", "maintenance", "offline")
EVENT_TYPES = ("order_created", "order_status_changed", "flight_created", "flight_status_changed")


def _in(*values: str) -> str:
    return ", ".join(f"'{value}'" for value in values)


_ALL_STATES = ", ".join(sorted({f"'{v}'" for v in (*ORDER_STATUSES, *FLIGHT_STATUSES)}))
_LIVE = _in("planned", "permission_granted", "mission_uploaded", "started", "paused")
_PERMISSION_REQUIRED = _in(
    "permission_granted",
    "mission_uploaded",
    "upload_failed",
    "started",
    "paused",
    "completed",
    "aborted",
)
_ACK_REQUIRED = _in("mission_uploaded", "started", "paused", "completed", "aborted")
_ACK_FORBIDDEN = _in("planned", "permission_granted", "permission_denied", "upload_failed")
_STARTED = _in("started", "paused", "completed", "aborted")
_WITH_REASON = _in("upload_failed", "cancelled", "aborted")

# CHECK constraints, keyed by the model's constraint name (the table prefix is added by the
# naming convention). tests/unit/test_migration_matches_models.py compares them to the models.
DRONE_CHECKS: dict[str, str] = {
    "name_not_blank": "btrim(name) <> ''",
    "status_valid": f"status IN ({_in(*DRONE_STATUSES)})",
    "max_payload_positive": "max_payload_kg > 0",
    "max_range_positive": "max_range_km > 0",
    "battery_percent_in_range": "battery_percent >= 0 AND battery_percent <= 100",
}

FLIGHT_CHECKS: dict[str, str] = {
    "status_valid": f"status IN ({_in(*FLIGHT_STATUSES)})",
    "mission_duration_positive": "mission_estimated_duration_s > 0",
    "permission_fields_together": (
        "(permission_id IS NULL) = (permission_issuer IS NULL)"
        " AND (permission_id IS NULL) = (permission_issued_at IS NULL)"
        " AND (permission_id IS NULL) = (permission_valid_from IS NULL)"
        " AND (permission_id IS NULL) = (permission_valid_until IS NULL)"
    ),
    "permission_required_once_granted": (
        f"status NOT IN ({_PERMISSION_REQUIRED}) OR permission_id IS NOT NULL"
    ),
    "no_permission_before_grant": (
        f"status NOT IN ({_in('planned', 'permission_denied')}) OR permission_id IS NULL"
    ),
    "ack_required_once_uploaded": (
        f"status NOT IN ({_ACK_REQUIRED}) OR acknowledged_checksum IS NOT NULL"
    ),
    "ack_forbidden_before_upload": (
        f"status NOT IN ({_ACK_FORBIDDEN}) OR acknowledged_checksum IS NULL"
    ),
    "ack_matches_mission": "acknowledged_checksum IS NULL OR acknowledged_checksum = mission_checksum",
    "started_at_matches_status": f"(status IN ({_STARTED})) = (started_at IS NOT NULL)",
    "reason_matches_status": f"(status IN ({_WITH_REASON})) = (failure_reason IS NOT NULL)",
    "denial_fields_together": "(denial_code IS NULL) = (denial_reason IS NULL)",
    "denial_matches_status": "(status = 'permission_denied') = (denial_code IS NOT NULL)",
}

# NB: the explicit IS NOT NULL tests are load-bearing. ``x IN (...)`` is NULL, not false, when
# x is NULL, and a CHECK constraint accepts NULL.
AUDIT_CHECKS: dict[str, str] = {
    "event_type_valid": f"event_type IN ({_in(*EVENT_TYPES)})",
    "previous_state_valid": f"previous_state IS NULL OR previous_state IN ({_ALL_STATES})",
    "new_state_valid": f"new_state IS NULL OR new_state IN ({_ALL_STATES})",
    "type_matches_states": (
        "(event_type = 'order_created' AND previous_state IS NULL AND new_state = 'created'"
        " AND flight_id IS NULL)"
        " OR (event_type = 'order_status_changed'"
        " AND previous_state IS NOT NULL AND new_state IS NOT NULL"
        f" AND previous_state IN ({_in(*ORDER_STATUSES)})"
        f" AND new_state IN ({_in(*ORDER_STATUSES)})"
        " AND previous_state <> new_state AND flight_id IS NULL)"
        " OR (event_type = 'flight_created' AND previous_state IS NULL"
        " AND new_state = 'planned' AND flight_id IS NOT NULL AND drone_id IS NOT NULL)"
        " OR (event_type = 'flight_status_changed'"
        " AND previous_state IS NOT NULL AND new_state IS NOT NULL"
        f" AND previous_state IN ({_in(*FLIGHT_STATUSES)})"
        f" AND new_state IN ({_in(*FLIGHT_STATUSES)})"
        " AND previous_state <> new_state AND flight_id IS NOT NULL AND drone_id IS NOT NULL)"
    ),
}

# What 0001 had, restored on downgrade.
_OLD_AUDIT_CHECKS: dict[str, str] = {
    "event_type_valid": "event_type IN ('order_created', 'order_status_changed')",
    "previous_state_valid": f"previous_state IS NULL OR previous_state IN ({_in(*ORDER_STATUSES)})",
    "new_state_valid": f"new_state IS NULL OR new_state IN ({_in(*ORDER_STATUSES)})",
    "type_matches_states": (
        "(event_type = 'order_created' AND previous_state IS NULL AND new_state = 'created')"
        " OR (event_type = 'order_status_changed' AND previous_state IS NOT NULL"
        " AND new_state IS NOT NULL AND previous_state <> new_state)"
    ),
}

FLIGHTS_LIFECYCLE_FUNCTION = """
CREATE FUNCTION flights_enforce_lifecycle() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'planned' THEN
            RAISE EXCEPTION 'flight % must be inserted in status planned, not %', NEW.id, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM audit_events
            WHERE flight_id = NEW.id
              AND event_type = 'flight_created'
              AND new_state = 'planned'
              AND recorded_at = now()
        ) THEN
            RAISE EXCEPTION 'flight % has no flight_created audit event from this transaction',
                NEW.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END IF;

    -- UPDATE: what a flight is about never changes.
    IF NEW.order_id <> OLD.order_id
       OR NEW.drone_id <> OLD.drone_id
       OR NEW.mission IS DISTINCT FROM OLD.mission
       OR NEW.mission_checksum <> OLD.mission_checksum
       OR NEW.mission_estimated_duration_s <> OLD.mission_estimated_duration_s THEN
        RAISE EXCEPTION 'flight %: its order, drone and mission are immutable', NEW.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Write-once facts: set only by the transition that establishes them.
    IF ROW(NEW.permission_id, NEW.permission_issuer, NEW.permission_issued_at,
           NEW.permission_valid_from, NEW.permission_valid_until)
       IS DISTINCT FROM
       ROW(OLD.permission_id, OLD.permission_issuer, OLD.permission_issued_at,
           OLD.permission_valid_from, OLD.permission_valid_until) THEN
        IF NOT (OLD.permission_id IS NULL
                AND OLD.status = 'planned' AND NEW.status = 'permission_granted') THEN
            RAISE EXCEPTION 'flight %: permission fields are write-once and set only when granted',
                NEW.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF ROW(NEW.denial_code, NEW.denial_reason) IS DISTINCT FROM ROW(OLD.denial_code, OLD.denial_reason) THEN
        IF NOT (OLD.denial_code IS NULL
                AND OLD.status = 'planned' AND NEW.status = 'permission_denied') THEN
            RAISE EXCEPTION 'flight %: denial fields are write-once and set only when denied',
                NEW.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.acknowledged_checksum IS DISTINCT FROM OLD.acknowledged_checksum THEN
        IF NOT (OLD.acknowledged_checksum IS NULL
                AND OLD.status = 'permission_granted' AND NEW.status = 'mission_uploaded') THEN
            RAISE EXCEPTION 'flight %: the mission ack is write-once and set only on upload',
                NEW.id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'flight %: started_at is write-once', NEW.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (
            SELECT 1 FROM flight_transitions
            WHERE from_status = OLD.status AND to_status = NEW.status
        ) THEN
            RAISE EXCEPTION 'flight % cannot move from % to %', NEW.id, OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM audit_events
            WHERE flight_id = NEW.id
              AND event_type = 'flight_status_changed'
              AND previous_state = OLD.status
              AND new_state = NEW.status
              AND recorded_at = now()
        ) THEN
            RAISE EXCEPTION 'flight % moved from % to % without an audit event from this transaction',
                NEW.id, OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        -- Rule 2: the gate for starting (and resuming) a flight.
        IF NEW.status = 'started' THEN
            IF NEW.permission_id IS NULL OR NEW.permission_valid_until IS NULL THEN
                RAISE EXCEPTION 'flight % cannot start without a flight permission', NEW.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.acknowledged_checksum IS NULL
               OR NEW.acknowledged_checksum <> NEW.mission_checksum THEN
                RAISE EXCEPTION 'flight % cannot start: the drone has not acknowledged the mission',
                    NEW.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.permission_valid_until <= clock_timestamp() THEN
                RAISE EXCEPTION 'flight % cannot start: its permission has expired', NEW.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.permission_valid_until
               < clock_timestamp() + make_interval(secs => NEW.mission_estimated_duration_s) THEN
                RAISE EXCEPTION 'flight % cannot start: its permission ends before the mission would finish',
                    NEW.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$
"""


def _create_checks(table: str, checks: dict[str, str]) -> None:
    for name, condition in checks.items():
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, sa.text(condition))


def _drop_checks(table: str, names: list[str]) -> None:
    for name in names:
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")


def upgrade() -> None:
    # --- drones -----------------------------------------------------------------------------
    op.create_table(
        "drones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "home",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("max_payload_kg", sa.Double(), nullable=False),
        sa.Column("max_range_km", sa.Double(), nullable=False),
        sa.Column("battery_percent", sa.Double(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drones")),
        sa.UniqueConstraint("name", name="uq_drones_name"),
    )
    _create_checks("drones", DRONE_CHECKS)
    op.create_index("ix_drones_status", "drones", ["status"])
    op.create_index("ix_drones_home", "drones", ["home"], postgresql_using="gist")

    # Orders may only reference registered drones from now on. Phase 2/3 could store any UUID,
    # so refuse (clearly) to migrate data that would violate the new foreign key.
    stray = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM orders WHERE drone_id IS NOT NULL"))
        .scalar_one()
    )
    if stray:
        raise RuntimeError(
            f"{stray} order(s) reference drone ids that are not registered; register those "
            "drones (or clear the references) before applying this migration"
        )
    op.create_foreign_key(
        op.f("fk_orders_drone_id_drones"),
        "orders",
        "drones",
        ["drone_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- flights ----------------------------------------------------------------------------
    op.create_table(
        "flights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("drone_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("mission", postgresql.JSONB(), nullable=False),
        sa.Column("mission_checksum", sa.Text(), nullable=False),
        sa.Column("mission_estimated_duration_s", sa.Double(), nullable=False),
        sa.Column("permission_id", sa.Text(), nullable=True),
        sa.Column("permission_issuer", sa.Text(), nullable=True),
        sa.Column("permission_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("permission_valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("permission_valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denial_code", sa.Text(), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("acknowledged_checksum", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_flights")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_flights_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["drone_id"],
            ["drones.id"],
            name=op.f("fk_flights_drone_id_drones"),
            ondelete="RESTRICT",
        ),
    )
    _create_checks("flights", FLIGHT_CHECKS)
    op.create_index("ix_flights_order_id", "flights", ["order_id"])
    op.create_index("ix_flights_status", "flights", ["status"])
    op.create_index(
        "uq_flights_live_per_drone",
        "flights",
        ["drone_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_LIVE})"),
    )
    op.create_index(
        "uq_flights_live_per_order",
        "flights",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_LIVE})"),
    )

    # --- audit_events: flight events --------------------------------------------------------
    op.add_column("audit_events", sa.Column("flight_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_events_flight_id_flights"),
        "audit_events",
        "flights",
        ["flight_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_audit_events_flight_id_seq", "audit_events", ["flight_id", "seq"])
    _drop_checks("audit_events", list(AUDIT_CHECKS))
    _create_checks("audit_events", AUDIT_CHECKS)

    # --- flight_transitions: the allowed edges, immutable ------------------------------------
    transitions = op.create_table(
        "flight_transitions",
        sa.Column("from_status", sa.Text(), nullable=False),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("from_status", "to_status", name=op.f("pk_flight_transitions")),
        sa.CheckConstraint(
            f"from_status IN ({_in(*FLIGHT_STATUSES)}) AND to_status IN ({_in(*FLIGHT_STATUSES)})",
            name=op.f("ck_flight_transitions_statuses_valid"),
        ),
    )
    op.bulk_insert(
        transitions,
        [{"from_status": src, "to_status": dst} for src, dst in FLIGHT_TRANSITIONS],
    )
    op.execute(
        "CREATE TRIGGER flight_transitions_reject_changes BEFORE INSERT OR UPDATE OR DELETE"
        " ON flight_transitions FOR EACH ROW EXECUTE FUNCTION reject_row_mutation()"
    )
    op.execute(
        "CREATE TRIGGER flight_transitions_reject_truncate BEFORE TRUNCATE"
        " ON flight_transitions FOR EACH STATEMENT EXECUTE FUNCTION reject_row_mutation()"
    )

    # --- the lifecycle guard -----------------------------------------------------------------
    op.execute(FLIGHTS_LIFECYCLE_FUNCTION)
    op.execute(
        "CREATE CONSTRAINT TRIGGER flights_enforce_lifecycle AFTER INSERT OR UPDATE ON flights"
        " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION flights_enforce_lifecycle()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    flight_events = bind.execute(
        sa.text("SELECT count(*) FROM audit_events WHERE event_type LIKE 'flight\\_%'")
    ).scalar_one()
    if flight_events:
        raise RuntimeError(
            f"cannot downgrade: audit_events holds {flight_events} flight event(s). The audit "
            "log is append-only, so this migration will not delete them."
        )

    op.execute("DROP TRIGGER flights_enforce_lifecycle ON flights")
    op.execute("DROP FUNCTION flights_enforce_lifecycle()")
    op.drop_table("flight_transitions")

    _drop_checks("audit_events", list(AUDIT_CHECKS))
    op.drop_index("ix_audit_events_flight_id_seq", table_name="audit_events")
    op.drop_constraint(
        op.f("fk_audit_events_flight_id_flights"), "audit_events", type_="foreignkey"
    )
    op.drop_column("audit_events", "flight_id")
    _create_checks("audit_events", _OLD_AUDIT_CHECKS)

    op.drop_table("flights")
    op.drop_constraint(op.f("fk_orders_drone_id_drones"), "orders", type_="foreignkey")
    op.drop_table("drones")
