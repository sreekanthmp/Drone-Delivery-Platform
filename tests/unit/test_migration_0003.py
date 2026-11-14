"""Migration 0003 is a frozen snapshot; keep it in lock-step with the models and the domain.

The migration holds literal copies of the status lists and the CHECK constraints, and
Alembic's autogenerate comparison does not look at CHECK constraints, so these tests compare
them directly. No database needed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import CheckConstraint

from app.db.models import WebhookAttemptRecord, WebhookDeliveryRecord
from app.domain.models import AttemptOutcome, DeliveryStatus

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "0003_webhook_deliveries.py"
)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def squash(sql: str) -> str:
    return " ".join(sql.split())


def model_checks(model: type) -> dict[str, str]:
    """The model's CHECK constraints keyed by their short name (``ck_<table>_`` removed)."""
    prefix = f"ck_{model.__tablename__}_"  # type: ignore[attr-defined]
    return {
        str(constraint.name).removeprefix(prefix): squash(str(constraint.sqltext))
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    }


def tokens(sql_list: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sql_list))


def test_it_follows_0002(migration: ModuleType) -> None:
    assert (migration.revision, migration.down_revision) == ("0003", "0002")


def test_delivery_statuses(migration: ModuleType) -> None:
    assert set(migration.DELIVERY_STATUSES) == {s.value for s in DeliveryStatus}


def test_attempt_outcomes(migration: ModuleType) -> None:
    assert set(migration.ATTEMPT_OUTCOMES) == {o.value for o in AttemptOutcome}


@pytest.mark.parametrize(
    ("model", "attribute"),
    [(WebhookDeliveryRecord, "DELIVERY_CHECKS"), (WebhookAttemptRecord, "ATTEMPT_CHECKS")],
    ids=["webhook_deliveries", "webhook_attempts"],
)
def test_every_check_constraint_matches_the_model_by_name_and_text(
    migration: ModuleType, model: type, attribute: str
) -> None:
    in_migration = {name: squash(sql) for name, sql in getattr(migration, attribute).items()}

    assert model_checks(model) == in_migration


def test_the_status_checks_list_every_status_the_domain_has(migration: ModuleType) -> None:
    assert tokens(migration.DELIVERY_CHECKS["status_valid"]) == {s.value for s in DeliveryStatus}
    assert tokens(migration.ATTEMPT_CHECKS["outcome_valid"]) == {o.value for o in AttemptOutcome}


def test_the_lease_rule_names_the_one_status_that_may_be_leased(migration: ModuleType) -> None:
    assert tokens(migration.DELIVERY_CHECKS["lease_only_pending"]) == {DeliveryStatus.PENDING.value}
