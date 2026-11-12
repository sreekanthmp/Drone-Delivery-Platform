"""Migration 0002 is a frozen snapshot; keep it in lock-step with the models and the domain.

Alembic's autogenerate comparison does not look at CHECK constraints, and the migration
holds literal copies of the status lists and the flight edges, so these tests compare them
directly. No database needed.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import CheckConstraint

from app.db.models import AuditEventRecord, DroneRecord, FlightRecord
from app.domain.flight_state_machine import ALLOWED_FLIGHT_TRANSITIONS
from app.domain.models import (
    LIVE_FLIGHT_STATES,
    AuditEventType,
    DroneStatus,
    FlightStatus,
    OrderStatus,
)

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "0002_drones_and_flights.py"
)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def squash(sql: str) -> str:
    return " ".join(sql.split())


def model_checks(model: type) -> dict[str, str]:
    """The model's CHECK constraints keyed by their full name (``ck_<table>_<name>``)."""
    return {
        str(constraint.name): squash(str(constraint.sqltext))
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    }


def tokens(sql_list: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sql_list))


class TestSnapshotsMatchTheDomain:
    def test_flight_transitions(self, migration: ModuleType) -> None:
        in_python = {
            (s.value, t.value) for s, targets in ALLOWED_FLIGHT_TRANSITIONS.items() for t in targets
        }
        assert set(migration.FLIGHT_TRANSITIONS) == in_python
        assert len(migration.FLIGHT_TRANSITIONS) == len(in_python) == 13

    def test_flight_statuses(self, migration: ModuleType) -> None:
        assert set(migration.FLIGHT_STATUSES) == {s.value for s in FlightStatus}

    def test_order_statuses(self, migration: ModuleType) -> None:
        assert set(migration.ORDER_STATUSES) == {s.value for s in OrderStatus}

    def test_drone_statuses(self, migration: ModuleType) -> None:
        assert set(migration.DRONE_STATUSES) == {s.value for s in DroneStatus}

    def test_event_types(self, migration: ModuleType) -> None:
        assert set(migration.EVENT_TYPES) == {t.value for t in AuditEventType}

    def test_the_live_statuses_behind_the_unique_indexes(self, migration: ModuleType) -> None:
        assert tokens(migration._LIVE) == {s.value for s in LIVE_FLIGHT_STATES}


class TestCheckConstraintsMatchTheModels:
    @pytest.mark.parametrize(
        ("model", "attribute"),
        [(DroneRecord, "DRONE_CHECKS"), (FlightRecord, "FLIGHT_CHECKS")],
        ids=["drones", "flights"],
    )
    def test_every_constraint_matches_by_name_and_text(
        self, migration: ModuleType, model: type, attribute: str
    ) -> None:
        in_migration = {name: squash(sql) for name, sql in getattr(migration, attribute).items()}
        in_model = {
            name.removeprefix(f"ck_{model.__tablename__}_"): sql  # type: ignore[attr-defined]
            for name, sql in model_checks(model).items()
        }

        assert in_model == in_migration

    def test_the_audit_constraints_rewritten_by_this_migration(self, migration: ModuleType) -> None:
        in_model = {
            name.removeprefix("ck_audit_events_"): sql
            for name, sql in model_checks(AuditEventRecord).items()
        }
        for name, sql in migration.AUDIT_CHECKS.items():
            assert in_model[name] == squash(sql), name

    def test_the_audit_constraint_names_are_all_accounted_for(self, migration: ModuleType) -> None:
        in_model = {
            name.removeprefix("ck_audit_events_") for name in model_checks(AuditEventRecord)
        }
        # source_valid is from 0001 and untouched; everything else is rewritten here.
        assert in_model == set(migration.AUDIT_CHECKS) | {"source_valid"}

    def test_the_audit_state_check_guards_against_null_passing(self, migration: ModuleType) -> None:
        """``x IN (...)`` is NULL for a NULL x and a CHECK accepts NULL: keep the explicit tests."""
        sql = migration.AUDIT_CHECKS["type_matches_states"]
        assert sql.count("previous_state IS NOT NULL AND new_state IS NOT NULL") == 2

    def test_downgrade_restores_the_original_audit_checks(self, migration: ModuleType) -> None:
        assert set(migration._OLD_AUDIT_CHECKS) == set(migration.AUDIT_CHECKS)
        assert "flight" not in migration._OLD_AUDIT_CHECKS["type_matches_states"]
        assert tokens(migration._OLD_AUDIT_CHECKS["event_type_valid"]) == {
            "order_created",
            "order_status_changed",
        }
