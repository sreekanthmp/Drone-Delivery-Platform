"""Architecture tests: the live WebSocket streams only ever read (Rule 1).

A stream sends what is happening. It must not be a way to command, steer or change anything:

* it never interprets what a client sends (the only look at the client is "did it disconnect?");
* the only service calls it makes are reads, from an explicit allow-list;
* the app exposes exactly the three streams planned, and none of them is a write route.

AST scans are heuristics; each detector is tested against code that breaks the rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.routing import WebSocketRoute

from app.config import Settings
from app.main import create_app
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

ROOT = Path(__file__).resolve().parents[2]
WS_DIR = ROOT / "app" / "api" / "ws"

# What each kind of collaborator may be asked: reads only.
ALLOWED_CALLS: dict[str, set[str]] = {
    "orders": {"get_order", "get_order_by_external_id"},
    "_orders": {"get_order"},
    "dispatch": {"list_drones", "list_flights_for_order"},
    "live": {"all_states", "active", "get_state"},
    "_live": {"get_state"},
}
CLIENT_INPUT_READERS = {"receive_json", "receive_text", "receive_bytes", "iter_json", "iter_text"}

EXPECTED_STREAMS = {
    "/ws/internal/fleet",
    "/ws/internal/orders/{order_id}",
    "/ws/partner/{client_id}/orders/{external_order_id}",
}


def ws_modules() -> list[Path]:
    return sorted(p for p in WS_DIR.glob("*.py") if p.name != "__init__.py")


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def disallowed_service_calls(source: str) -> list[str]:
    """Calls on the collaborators above that are not on its allow-list."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = _receiver(node.func.value)
            if receiver in ALLOWED_CALLS and node.func.attr not in ALLOWED_CALLS[receiver]:
                found.append(f"line {node.lineno}: {receiver}.{node.func.attr}")
    return found


def reads_client_input(source: str) -> list[str]:
    return [
        f"line {node.lineno}: {node.attr}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in CLIENT_INPUT_READERS
    ]


def declares_writes(source: str) -> list[str]:
    """HTTP write decorators (a stream module should declare only websocket routes)."""
    return [
        f"line {node.lineno}: {node.attr}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr in {"post", "put", "patch", "delete"}
        and _receiver(node.value) == "router"
    ]


def received_message_uses(source: str) -> list[str]:
    """Reads of a message returned by ``receive()`` other than its ``type``."""
    tree = ast.parse(source)
    received: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and isinstance(node.value.value.func, ast.Attribute)
            and node.value.value.func.attr == "receive"
        ):
            received |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            index = node.slice
            if node.value.id in received and not (
                isinstance(index, ast.Constant) and index.value == "type"
            ):
                problems.append(f"line {node.lineno}: reads more than the message type")
    return problems


@pytest.mark.parametrize("path", ws_modules(), ids=lambda p: p.name)
class TestEveryStreamModule:
    def test_it_makes_only_read_calls(self, path: Path) -> None:
        assert disallowed_service_calls(path.read_text()) == []

    def test_it_never_reads_what_a_client_sends(self, path: Path) -> None:
        assert reads_client_input(path.read_text()) == []

    def test_it_declares_no_http_write_route(self, path: Path) -> None:
        assert declares_writes(path.read_text()) == []


def test_the_scan_covers_the_stream_modules() -> None:
    assert {p.name for p in ws_modules()} >= {
        "internal.py",
        "partner.py",
        "streaming.py",
        "tracker.py",
    }


def test_the_only_look_at_the_client_is_whether_it_has_disconnected() -> None:
    source = (WS_DIR / "streaming.py").read_text()

    assert "websocket.disconnect" in source
    assert received_message_uses(source) == []
    assert "Anything else the client sends is ignored" in source


def test_the_read_calls_the_streams_do_make_are_all_on_the_allow_list() -> None:
    """Guards the allow-list against going stale: each module's reads are covered by it."""
    used: set[str] = set()
    for path in ws_modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = _receiver(node.func.value)
                if receiver in ALLOWED_CALLS:
                    used.add(f"{receiver}.{node.func.attr}")
    expected = {f"{r}.{m}" for r, methods in ALLOWED_CALLS.items() for m in methods}

    assert used == expected


@pytest.fixture
def app() -> FastAPI:
    database = InMemoryDatabase()
    return create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        uow_factory=lambda: InMemoryUnitOfWork(database),
    )


def websocket_routes(app: FastAPI) -> list[WebSocketRoute]:
    """Every WebSocket route, including those inside included routers (which a flat scan of
    ``app.routes`` does not show)."""
    found: list[WebSocketRoute] = []
    for route in app.routes:
        nested = getattr(route, "original_router", None)
        candidates = nested.routes if nested is not None else [route]
        found += [r for r in candidates if isinstance(r, WebSocketRoute)]
    return found


def test_the_app_exposes_exactly_the_planned_streams(app: FastAPI) -> None:
    assert {route.path for route in websocket_routes(app)} == EXPECTED_STREAMS


def test_the_route_scan_sees_nothing_vacuously(app: FastAPI) -> None:
    assert len(websocket_routes(app)) == 3


class TestTheDetectorsThemselves:
    @pytest.mark.parametrize(
        "source",
        [
            "await dispatch.cancel_order(i)",
            "await dispatch.return_to_home(f)",
            "await orders.transition(i, s)",
            "await live.raise_alert(a)",
            "await live.set_state(s)",
            "await self._orders.transition(i, s)",
            "await self._live.publish(c, e)",
        ],
    )
    def test_flags_a_write_through_a_collaborator(self, source: str) -> None:
        assert disallowed_service_calls(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "await orders.get_order(i)",
            "await dispatch.list_drones()",
            "await live.all_states()",
            "await live.active()",
            "await self._live.get_state(d)",
            "x = hub.subscribe(c)",
        ],
    )
    def test_allows_reads(self, source: str) -> None:
        assert disallowed_service_calls(source) == []

    @pytest.mark.parametrize(
        "source",
        [
            "data = await websocket.receive_json()",
            "t = await ws.receive_text()",
            "async for m in ws.iter_json(): pass",
        ],
    )
    def test_flags_reading_what_a_client_sends(self, source: str) -> None:
        assert reads_client_input(source) != []

    def test_allows_looking_only_at_the_message_type(self) -> None:
        source = "message = await ws.receive()\nif message['type'] == 'websocket.disconnect': pass"
        assert reads_client_input(source) == []
        assert received_message_uses(source) == []

    def test_flags_reading_the_payload_of_a_received_message(self) -> None:
        source = "message = await ws.receive()\ncommand = message['text']"
        assert received_message_uses(source) != []

    def test_flags_a_stream_module_that_declares_a_write_route(self) -> None:
        assert declares_writes("@router.post('/x')\nasync def f(): ...") != []
        assert declares_writes("@router.websocket('/x')\nasync def f(): ...") == []
