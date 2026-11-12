"""The migration is a frozen snapshot; make sure it has not drifted from the domain.

These checks need no database: they import the migration module and compare its
literal lists with the domain definitions. (The integration suite additionally
checks the rows that actually ended up in PostgreSQL.)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.domain.models import AuditEventType, EventSource, OrderStatus
from app.domain.order_state_machine import ALLOWED_TRANSITIONS

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "0001_initial_schema.py"
)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0001", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transition_table_matches_the_state_machine(migration: ModuleType) -> None:
    from_python = {
        (s.value, t.value) for s, targets in ALLOWED_TRANSITIONS.items() for t in targets
    }

    assert set(migration.ORDER_TRANSITIONS) == from_python
    assert len(migration.ORDER_TRANSITIONS) == len(from_python), "no duplicate edges"
    assert len(from_python) == 17


def test_statuses_match_the_domain(migration: ModuleType) -> None:
    assert set(migration.STATUSES) == {s.value for s in OrderStatus}


def test_event_types_are_the_phase_2_snapshot(migration: ModuleType) -> None:
    """0001 is frozen history: the two order event types. Flight events arrive in 0002."""
    assert set(migration.EVENT_TYPES) == {"order_created", "order_status_changed"}
    assert set(migration.EVENT_TYPES) < {t.value for t in AuditEventType}


def test_sources_match_the_domain(migration: ModuleType) -> None:
    assert set(migration.SOURCES) == {s.value for s in EventSource}
