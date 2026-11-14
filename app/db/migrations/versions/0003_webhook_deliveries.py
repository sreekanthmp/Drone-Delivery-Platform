"""Webhook deliveries and their attempts (Phase 6).

Hand-written, like 0001 and 0002. What it adds:

* ``webhook_deliveries``: the queue of notifications owed to partners. One row per order audit
  event, keyed by that event's id (so an event is queued at most once), written by the order
  store in the same transaction as the event. ``seq`` is insertion order, which is the order an
  order's updates are sent in. ``CHECK`` constraints mirror the domain's rules for a delivery.
* ``webhook_attempts``: every HTTP try at a delivery, for failure tracking. Insert-only: the same
  ``reject_row_mutation`` trigger as the audit log rejects ``UPDATE``, ``DELETE`` and
  ``TRUNCATE``.

Orders that exist before this migration have no deliveries and get none: the feature reports
changes from now on, it does not replay history.

Downgrade refuses to run if any attempt has been recorded: the attempt history is append-only,
so a migration will not delete it.

Each ``op.execute`` is a single statement (asyncpg cannot prepare multi-statement strings).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Frozen snapshots. Unit tests fail if any of these drifts from the domain or the models.
DELIVERY_STATUSES = ("pending", "delivered", "failed", "skipped")
ATTEMPT_OUTCOMES = ("delivered", "retry", "failed")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


DELIVERY_CHECKS: dict[str, str] = {
    "status_valid": f"status IN ({_in_list(DELIVERY_STATUSES)})",
    "client_id_not_empty": "client_id <> ''",
    "attempts_valid": "attempts >= 0 AND round_start >= 0 AND round_start <= attempts",
    "status_code_valid": "last_status_code IS NULL OR last_status_code BETWEEN 100 AND 599",
    "delivered_at_matches": "(status = 'delivered') = (delivered_at IS NOT NULL)",
    "lease_only_pending": "lease_until IS NULL OR status = 'pending'",
}

ATTEMPT_CHECKS: dict[str, str] = {
    "outcome_valid": f"outcome IN ({_in_list(ATTEMPT_OUTCOMES)})",
    "attempt_no_valid": "attempt_no >= 1",
    "duration_valid": "duration_ms >= 0",
    "status_code_valid": "status_code IS NULL OR status_code BETWEEN 100 AND 599",
}


def _create_checks(table: str, checks: dict[str, str]) -> None:
    for name, condition in checks.items():
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, sa.text(condition))


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("round_start", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_webhook_deliveries")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["audit_events.event_id"],
            name=op.f("fk_webhook_deliveries_event_id_audit_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_webhook_deliveries_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("seq", name="uq_webhook_deliveries_seq"),
    )
    _create_checks("webhook_deliveries", DELIVERY_CHECKS)
    op.create_index("ix_webhook_deliveries_order_id_seq", "webhook_deliveries", ["order_id", "seq"])
    op.create_index("ix_webhook_deliveries_status_seq", "webhook_deliveries", ["status", "seq"])
    op.create_index(
        "ix_webhook_deliveries_client_id_seq", "webhook_deliveries", ["client_id", "seq"]
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "webhook_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_attempts")),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["webhook_deliveries.event_id"],
            name=op.f("fk_webhook_attempts_event_id_webhook_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("event_id", "attempt_no", name="uq_webhook_attempts_event_attempt"),
    )
    _create_checks("webhook_attempts", ATTEMPT_CHECKS)

    # The attempt history is append-only, like the audit log (the function comes from 0001).
    op.execute(
        "CREATE TRIGGER webhook_attempts_reject_update_delete BEFORE UPDATE OR DELETE"
        " ON webhook_attempts FOR EACH ROW EXECUTE FUNCTION reject_row_mutation()"
    )
    op.execute(
        "CREATE TRIGGER webhook_attempts_reject_truncate BEFORE TRUNCATE"
        " ON webhook_attempts FOR EACH STATEMENT EXECUTE FUNCTION reject_row_mutation()"
    )


def downgrade() -> None:
    recorded = op.get_bind().execute(sa.text("SELECT count(*) FROM webhook_attempts")).scalar_one()
    if recorded:
        raise RuntimeError(
            f"cannot downgrade: webhook_attempts holds {recorded} attempt(s). The attempt "
            "history is append-only, so this migration will not delete it."
        )
    op.drop_table("webhook_attempts")
    op.drop_table("webhook_deliveries")
