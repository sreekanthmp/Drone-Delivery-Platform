"""Architecture test: what the webhook layer may touch (Rules 1, 3 and 4).

``test_import_boundaries.py`` already stops ``app/webhooks`` importing the drone link, MQTT,
compliance, the database layer or the order service. This looks at what the code *does* with
what it may import (AST based, so comments and docstrings do not count):

* it only **reads** orders and audit events (``get``), and never reaches flights or drones;
* the only writes are to its own deliveries, through the three methods meant for it;
* no identifier names a drone, flight, mission, permission, link or broker (Rules 1 and 2);
* ``httpx`` is imported in exactly one module, so there is one place a webhook leaves the
  process, and its limits (timeout, no redirects) cannot be bypassed by a second poster.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBHOOKS = ROOT / "app" / "webhooks"
MODULES = sorted(p for p in WEBHOOKS.rglob("*.py") if p.name != "__main__.py")

# What the sender may call on each store of the unit of work.
READ_ONLY = {"orders": {"get"}, "audit": {"get"}}
FORBIDDEN_STORES = {"flights", "drones"}
DELIVERY_WRITES = {"claim_due", "apply"}
FORBIDDEN_WORDS = re.compile(
    r"drone|flight|mission|permission|compliance|mqtt|waypoint|telemetry|dispatch|battery",
    re.IGNORECASE,
)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def store_calls(source: str) -> list[tuple[str, str]]:
    """``(store, method)`` for every ``<x>.<store>.<method>`` access in the source."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in {*READ_ONLY, *FORBIDDEN_STORES, "webhooks"}
        ):
            found.append((node.value.attr, node.attr))
    return found


def identifiers(source: str) -> list[str]:
    """Every name the code uses (not strings, comments or docstrings)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.extend(alias.name for alias in node.names)
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
    return names


def find_violations(source: str) -> list[str]:
    problems: list[str] = []
    for store, method in store_calls(source):
        if store in FORBIDDEN_STORES:
            problems.append(f"reaches the {store} store ({method})")
        elif store in READ_ONLY and method not in READ_ONLY[store]:
            problems.append(f"{store}.{method}: orders and audit events are read-only here")
    for name in identifiers(source):
        # the one allowed mention: the module path of the order-events adapter helper
        if FORBIDDEN_WORDS.search(name):
            problems.append(f"names '{name}'")
    return problems


def test_the_scan_covers_the_package() -> None:
    names = {_relative(p) for p in MODULES}
    assert {
        "app/webhooks/sender.py",
        "app/webhooks/http.py",
        "app/webhooks/retry.py",
        "app/webhooks/signing.py",
        "app/webhooks/endpoints.py",
    } <= names


@pytest.mark.parametrize("path", MODULES, ids=_relative)
def test_the_webhook_layer_only_reads_orders_and_never_touches_drones(path: Path) -> None:
    assert find_violations(path.read_text()) == []


def test_the_sender_actually_reads_orders_and_audit_events_and_writes_its_own_deliveries() -> None:
    """Guards the scan above against matching nothing because the code moved."""
    calls = set(store_calls((WEBHOOKS / "sender.py").read_text()))

    assert {("orders", "get"), ("audit", "get")} <= calls
    assert {("webhooks", "claim_due"), ("webhooks", "apply")} <= calls
    assert {method for store, method in calls if store == "webhooks"} == DELIVERY_WRITES


def test_httpx_is_imported_in_exactly_one_module() -> None:
    importers = [
        _relative(p)
        for p in sorted((ROOT / "app").rglob("*.py"))
        if any(name.split(".")[0] == "httpx" for name in _imports(p.read_text()))
    ]

    assert importers == ["app/webhooks/http.py"]


def _imports(source: str) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.append(node.module or "")
    return modules


class TestTheDetectorItself:
    @pytest.mark.parametrize(
        "source",
        [
            "await uow.orders.apply_transition(result)",
            "await uow.orders.insert(result)",
            "await uow.audit.append(event)",
            "await uow.flights.get(flight_id)",
            "await uow.drones.list_available()",
            "link = DroneLink()",
            "from app.dispatch.link import DroneLink",
            "x = permission_id",
            "mqtt_client.publish(topic)",
            "def start_mission(): ...",
            "battery = 5",
        ],
    )
    def test_flags_what_the_layer_must_not_do(self, source: str) -> None:
        assert find_violations(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "order = await uow.orders.get(order_id)",
            "event = await uow.audit.get(event_id)",
            "await uow.webhooks.claim_due(now=n, lease_until=n, limit=1)",
            "await uow.webhooks.apply(before, after, attempt)",
            'def f():\n    """A drone flies a mission in prose."""\n',
            "# a drone, in a comment\nx = 1",
        ],
    )
    def test_allows_what_the_layer_needs(self, source: str) -> None:
        assert find_violations(source) == []
