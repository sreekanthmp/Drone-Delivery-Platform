"""Architecture test for Rule 4: order status is never written outside the state machine path.

The only legitimate path is::

    OrderStateMachine.create/transition  ->  TransitionResult
        ->  OrderService  ->  SqlAlchemyOrderStore.insert/apply_transition

This scans every module under ``app/`` (AST based, so comments and docstrings do not
count) and fails if code outside that path:

* assigns ``<something>.status = ...`` (or ``setattr(x, "status", ...)``),
* touches the ``OrderRecord`` / ``AuditEventRecord`` table classes,
* contains raw SQL that writes to the order or audit tables,
* calls ``Order.rehydrate`` (persistence-only) or ``dataclasses.replace(..., status=...)``,
* calls a store's write methods (``orders``/``flights`` ``insert`` and ``apply_transition``,
  ``drones`` ``add`` and ``apply_status_change``) outside the one service allowed to,
* creates a ``StartAuthorization`` anywhere but the flight state machine (Rule 2's token).

It is a heuristic guard-rail, not a proof: the database triggers are the backstop for
anything an AST scan cannot see.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"

_MODELS = "app/db/models/__init__.py"
_MIGRATION_ENV = "app/db/migrations/env.py"  # imports the tables only to register them
RECORD_CLASS_HOMES: dict[str, set[str]] = {
    "OrderRecord": {
        _MODELS,
        "app/db/models/order.py",
        "app/db/repositories/orders.py",
        _MIGRATION_ENV,
    },
    "AuditEventRecord": {
        _MODELS,
        "app/db/models/audit_event.py",
        "app/db/repositories/audit.py",
        _MIGRATION_ENV,
    },
    "DroneRecord": {
        _MODELS,
        "app/db/models/drone.py",
        "app/db/repositories/drones.py",
        _MIGRATION_ENV,
    },
    "FlightRecord": {
        _MODELS,
        "app/db/models/flight.py",
        "app/db/repositories/flights.py",
        _MIGRATION_ENV,
    },
    "WebhookDeliveryRecord": {
        _MODELS,
        "app/db/models/webhook.py",
        "app/db/repositories/webhooks.py",
        _MIGRATION_ENV,
    },
    "WebhookAttemptRecord": {
        _MODELS,
        "app/db/models/webhook.py",
        "app/db/repositories/webhooks.py",
        _MIGRATION_ENV,
    },
}
REHYDRATE_HOME_PREFIX = "app/db/"
# Only these modules may derive an entity with a different status via dataclasses.replace.
REPLACE_STATUS_HOMES = {
    "app/domain/order_state_machine.py",
    "app/domain/flight_state_machine.py",
    "app/domain/models/drone.py",
    # A webhook delivery has its own status (pending, delivered, failed, skipped). It is not an
    # order status, and the delivery model is the only place that changes it.
    "app/domain/models/webhook.py",
}
# Who may call each store's write methods: the order service for orders, and the dispatch
# service for flights and drones (it is the only code that may write them).
# Webhook deliveries are queued only by the order stores (in the transaction that writes the
# order's audit event, so a change cannot exist without its notification) and claimed and
# recorded only by the sender and the webhook service. ``_webhooks`` is how the order stores
# reach their own webhook store.
_WEBHOOK_WRITERS = {
    "app/db/repositories/orders.py",
    "app/services/memory.py",
    "app/webhooks/sender.py",
    "app/services/webhook_service.py",
}
STORE_WRITE_HOMES: dict[str, set[str]] = {
    "orders": {"app/services/order_service.py"},
    "flights": {"app/dispatch/service.py"},
    "drones": {"app/dispatch/service.py"},
    "webhooks": _WEBHOOK_WRITERS,
    "_webhooks": _WEBHOOK_WRITERS,
}
STORE_WRITE_METHODS: dict[str, set[str]] = {
    "orders": {"insert", "apply_transition"},
    "flights": {"insert", "apply_transition"},
    "drones": {"add", "apply_status_change", "set_battery"},
    "webhooks": {"add", "claim_due", "apply"},
    "_webhooks": {"add", "claim_due", "apply"},
}
# Only the flight state machine may mint the token that authorises a flight to start.
START_AUTHORIZATION_HOME = "app/domain/flight_state_machine.py"
SQL_EXEMPT_PREFIX = "app/db/migrations/"

RAW_SQL_WRITE = re.compile(
    r"\b(UPDATE|DELETE\s+FROM|INSERT\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+"
    r"(orders|audit_events|order_transitions|flights|flight_transitions|drones"
    r"|webhook_deliveries|webhook_attempts)\b",
    re.IGNORECASE,
)


def _docstring_constants(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _flatten(targets: list[ast.expr]) -> list[ast.expr]:
    flat: list[ast.expr] = []
    for target in targets:
        if isinstance(target, ast.Tuple | ast.List):
            flat.extend(_flatten(list(target.elts)))
        else:
            flat.append(target)
    return flat


def _is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in {"self", "cls"}


def find_violations(source: str, path: str) -> list[str]:
    """Return a description of every Rule 4 violation in ``source`` (``path`` is repo-relative)."""
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        # 1. status assignment
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            raw = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in _flatten(list(raw)):
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and not _is_self(target.value)
                ):
                    violations.append(f"line {node.lineno}: assigns .status")

        if isinstance(node, ast.Call):
            func = node.func
            # setattr(x, "status", ...)
            if (
                isinstance(func, ast.Name)
                and func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "status"
                and not _is_self(node.args[0])
            ):
                violations.append(f"line {node.lineno}: setattr(..., 'status', ...)")
            # Order.rehydrate(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "rehydrate"
                and not path.startswith(REHYDRATE_HOME_PREFIX)
            ):
                violations.append(f"line {node.lineno}: calls rehydrate outside app/db")
            # replace(order, status=...)
            is_replace = (isinstance(func, ast.Name) and func.id == "replace") or (
                isinstance(func, ast.Attribute) and func.attr == "replace"
            )
            if (
                is_replace
                and any(keyword.arg == "status" for keyword in node.keywords)
                and path not in REPLACE_STATUS_HOMES
            ):
                violations.append(
                    f"line {node.lineno}: replace(..., status=...) outside the state machine"
                )
            # store write methods: judged by which store they are called on
            if isinstance(func, ast.Attribute):
                receiver = func.value.attr if isinstance(func.value, ast.Attribute) else None
                if receiver in STORE_WRITE_METHODS and func.attr in STORE_WRITE_METHODS[receiver]:
                    if path not in STORE_WRITE_HOMES[receiver]:
                        violations.append(
                            f"line {node.lineno}: calls {receiver}.{func.attr} outside "
                            f"{sorted(STORE_WRITE_HOMES[receiver])}"
                        )
                elif func.attr == "apply_transition" and path not in {
                    home for homes in STORE_WRITE_HOMES.values() for home in homes
                }:
                    # a store reached through some other name: still not allowed
                    violations.append(
                        f"line {node.lineno}: calls apply_transition outside the services"
                    )
            # StartAuthorization(...) may only be created by the flight state machine
            constructor = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if constructor == "StartAuthorization" and path != START_AUTHORIZATION_HOME:
                violations.append(
                    f"line {node.lineno}: creates a StartAuthorization outside the flight "
                    "state machine"
                )

        # 2. table classes
        if (
            isinstance(node, ast.Name)
            and node.id in RECORD_CLASS_HOMES
            and path not in RECORD_CLASS_HOMES[node.id]
        ):
            violations.append(f"line {node.lineno}: uses {node.id} outside its repository")
        if (
            isinstance(node, ast.Attribute)
            and node.attr in RECORD_CLASS_HOMES
            and path not in RECORD_CLASS_HOMES[node.attr]
        ):
            violations.append(f"line {node.lineno}: uses {node.attr} outside its repository")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in RECORD_CLASS_HOMES and path not in RECORD_CLASS_HOMES[alias.name]:
                    violations.append(
                        f"line {node.lineno}: imports {alias.name} outside its repository"
                    )

        # 3. raw SQL
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and not path.startswith(SQL_EXEMPT_PREFIX)
            and RAW_SQL_WRITE.search(node.value)
        ):
            violations.append(f"line {node.lineno}: raw SQL writes to a guarded table")

    return violations


def app_modules() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_scan_covers_the_modules_that_matter() -> None:
    names = {_relative(p) for p in app_modules()}
    assert {
        "app/db/repositories/orders.py",
        "app/db/repositories/audit.py",
        "app/services/order_service.py",
        "app/domain/order_state_machine.py",
        "app/db/migrations/versions/0001_initial_schema.py",
        "app/db/migrations/versions/0002_drones_and_flights.py",
        "app/db/repositories/flights.py",
        "app/db/repositories/drones.py",
        "app/domain/flight_state_machine.py",
        "app/domain/models/drone.py",
        "app/db/migrations/versions/0003_webhook_deliveries.py",
        "app/db/repositories/webhooks.py",
        "app/domain/models/webhook.py",
        "app/webhooks/sender.py",
    } <= names


@pytest.mark.parametrize("path", app_modules(), ids=_relative)
def test_module_has_no_direct_status_writes(path: Path) -> None:
    assert find_violations(path.read_text(), _relative(path)) == []


class TestTheDetectorItself:
    @pytest.mark.parametrize(
        ("source", "path"),
        [
            ("order.status = OrderStatus.DELIVERED", "app/api/internal/orders.py"),
            ("record.status = 'delivered'", "app/telemetry/mqtt_ingest.py"),
            ("order.status += 1", "app/dispatch/assignment.py"),
            ("a, order.status = 1, 2", "app/dispatch/assignment.py"),
            ("order.status: str = 'x'", "app/dispatch/assignment.py"),
            ("setattr(order, 'status', s)", "app/api/internal/orders.py"),
            ("from app.db.models import OrderRecord", "app/services/other.py"),
            ("session.add(OrderRecord(id=1))", "app/api/internal/orders.py"),
            ("stmt = update(OrderRecord).values(status='x')", "app/telemetry/mqtt_ingest.py"),
            ("import app.db.models as m\nm.OrderRecord", "app/api/ops/dash.py"),
            ("from app.db.models import AuditEventRecord", "app/api/ops/dash.py"),
            ("delete(AuditEventRecord)", "app/webhooks/sender.py"),
            ("db.execute(\"UPDATE orders SET status = 'x'\")", "app/api/internal/orders.py"),
            ("db.execute('delete from audit_events')", "app/api/internal/orders.py"),
            ("db.execute('INSERT INTO orders VALUES (1)')", "app/dispatch/mission.py"),
            ("db.execute('TRUNCATE TABLE audit_events')", "app/dispatch/mission.py"),
            ("db.execute(f'UPDATE orders SET status = {x}')", "app/dispatch/mission.py"),
            ("Order.rehydrate(id=1)", "app/api/internal/orders.py"),
            ("order = dataclasses.replace(order, status=s)", "app/dispatch/assignment.py"),
            ("order = replace(order, status=s)", "app/dispatch/assignment.py"),
            ("await uow.orders.apply_transition(result)", "app/telemetry/mqtt_ingest.py"),
            ("await uow.orders.insert(result)", "app/api/internal/orders.py"),
            ("await uow.flights.apply_transition(result)", "app/api/internal/flights.py"),
            ("await uow.flights.insert(result)", "app/services/order_service.py"),
            ("await uow.flights.apply_transition(r)", "app/telemetry/mqtt_ingest.py"),
            ("await uow.drones.apply_status_change(a, b)", "app/api/internal/drones.py"),
            ("await uow.drones.add(drone)", "app/services/order_service.py"),
            ("await uow.orders.apply_transition(r)", "app/dispatch/service.py"),
            ("await store.apply_transition(r)", "app/api/internal/orders.py"),
            ("auth = StartAuthorization(flight_id=f)", "app/dispatch/service.py"),
            ("auth = flight_state_machine.StartAuthorization(f)", "app/api/internal/flights.py"),
            ("db.execute('UPDATE flights SET status = 1')", "app/dispatch/service.py"),
            ("db.execute('DELETE FROM drones')", "app/api/internal/drones.py"),
            ("db.execute('insert into flight_transitions values (1)')", "app/dispatch/mission.py"),
            ("from app.db.models import FlightRecord", "app/dispatch/service.py"),
            ("from app.db.models import DroneRecord", "app/dispatch/assignment.py"),
            ("flight = Flight.rehydrate(id=1)", "app/dispatch/service.py"),
            ("drone = replace(drone, status=DroneStatus.AVAILABLE)", "app/dispatch/service.py"),
            ("flight = replace(flight, status=FlightStatus.STARTED)", "app/dispatch/service.py"),
            ("await uow.webhooks.add(event, client_id)", "app/api/internal/orders.py"),
            (
                "await uow.webhooks.claim_due(now=n, lease_until=n, limit=1)",
                "app/dispatch/service.py",
            ),
            ("await uow.webhooks.apply(a, b, None)", "app/telemetry/ingest.py"),
            ("await self._webhooks.add(event, client_id)", "app/dispatch/service.py"),
            ("from app.db.models import WebhookDeliveryRecord", "app/api/internal/webhooks.py"),
            ("db.execute('DELETE FROM webhook_attempts')", "app/webhooks/sender.py"),
            ("db.execute('update webhook_deliveries set status = 1')", "app/api/x.py"),
            ("d = replace(delivery, status=DeliveryStatus.DELIVERED)", "app/webhooks/sender.py"),
        ],
    )
    def test_flags_writes_outside_the_blessed_path(self, source: str, path: str) -> None:
        assert find_violations(source, path) != []

    @pytest.mark.parametrize(
        ("source", "path"),
        [
            ("self.status = 'connected'", "app/telemetry/mqtt_ingest.py"),
            ("setattr(self, 'status', 'x')", "app/dispatch/assignment.py"),
            ("current = order.status", "app/api/internal/orders.py"),
            ("if order.status is OrderStatus.CREATED: ...", "app/api/internal/orders.py"),
            ('def f():\n    """Runs UPDATE orders SET status = 1 in prose."""\n', "app/api/x.py"),
            ("stmt = update(OrderRecord)", "app/db/repositories/orders.py"),
            ("x = AuditEventRecord", "app/db/repositories/audit.py"),
            ("Order.rehydrate(id=1)", "app/db/repositories/orders.py"),
            ("db.execute('UPDATE orders SET x = 1')", "app/db/migrations/versions/0002_x.py"),
            ("o = replace(order, drone_id=None)", "app/dispatch/assignment.py"),
            ("'a-b'.replace('-', '_')", "app/api/x.py"),
            ("o = replace(order, status=s)", "app/domain/order_state_machine.py"),
            ("await uow.orders.apply_transition(r)", "app/services/order_service.py"),
            ("await uow.orders.insert(r)", "app/services/order_service.py"),
            ("await uow.flights.apply_transition(r)", "app/dispatch/service.py"),
            ("await uow.flights.insert(r)", "app/dispatch/service.py"),
            ("await uow.drones.apply_status_change(a, b)", "app/dispatch/service.py"),
            ("await uow.drones.add(drone)", "app/dispatch/service.py"),
            ("await uow.flights.get(flight_id)", "app/api/internal/flights.py"),
            ("await uow.flights.list_for_order(order_id)", "app/api/internal/flights.py"),
            ("await uow.drones.list_available(lock=True)", "app/dispatch/service.py"),
            ("await uow.drones.get(drone_id)", "app/api/internal/drones.py"),
            ("session.add(thing)", "app/api/internal/orders.py"),
            ("items.insert(0, x)", "app/api/internal/orders.py"),
            ("auth = StartAuthorization(flight_id=f)", "app/domain/flight_state_machine.py"),
            ("flight = replace(flight, status=target)", "app/domain/flight_state_machine.py"),
            ("drone = replace(drone, status=target)", "app/domain/models/drone.py"),
            ("flight = Flight.rehydrate(id=1)", "app/db/repositories/flights.py"),
            ("db.execute('SELECT * FROM flights')", "app/api/internal/flights.py"),
            ("db.execute('UPDATE flights SET x = 1')", "app/db/migrations/versions/0003_x.py"),
            ("await uow.orders.get(order_id)", "app/api/internal/orders.py"),
            ("await uow.audit.list_for_order(order_id)", "app/api/ops/dash.py"),
            ("db.execute('SELECT * FROM orders')", "app/api/ops/dash.py"),
            ("db.execute('SELECT * FROM audit_events')", "app/api/ops/dash.py"),
            ("await self._webhooks.add(event, client_id)", "app/db/repositories/orders.py"),
            ("await self._webhooks.add(event, client_id)", "app/services/memory.py"),
            (
                "await uow.webhooks.claim_due(now=n, lease_until=n, limit=1)",
                "app/webhooks/sender.py",
            ),
            ("await uow.webhooks.apply(a, b, None)", "app/webhooks/sender.py"),
            ("await uow.webhooks.get(event_id)", "app/api/internal/webhooks.py"),
            ("await uow.webhooks.list_recent(limit=10)", "app/api/internal/webhooks.py"),
            (
                "d = replace(delivery, status=DeliveryStatus.PENDING)",
                "app/domain/models/webhook.py",
            ),
            ("x = WebhookDeliveryRecord", "app/db/repositories/webhooks.py"),
            ("db.execute('SELECT * FROM webhook_deliveries')", "app/api/internal/webhooks.py"),
        ],
    )
    def test_allows_legitimate_code(self, source: str, path: str) -> None:
        assert find_violations(source, path) == []
