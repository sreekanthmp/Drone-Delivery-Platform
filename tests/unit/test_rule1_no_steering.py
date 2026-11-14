"""Architecture tests for Rule 1: the cloud never steers a drone.

The platform may only (a) upload a predefined mission and (b) send high-level commands:
start, pause, return-to-home, land. Concretely this is enforced by:

* the drone link port having exactly those five operations, none of which can carry a
  position, a heading, a speed or a status (parameters are only ids, a mission, and the
  Rule 2 start authorisation);
* only the dispatch service ever calling a link operation, and only once, so that every
  command passes through the code that orders and records it;
* nothing in the code base being *named* like a steering primitive;
* the safety commands (pause, return-to-home, land, cancellation recall) being structurally
  unable to consult the compliance provider (stopping is never gated), and the start
  command being sent strictly after the gated transition is recorded (Rule 2).

AST scans are heuristics: they catch the way a mistake would actually be made, and each
detector is itself tested against code that breaks the rule. The behavioural tests in
``test_dispatch_service.py`` and the OpenAPI test cover the same rules from the outside.
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest

from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.link import DroneLink, MissionAck
from app.domain.flight_state_machine import StartAuthorization
from app.domain.models import Mission

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
SERVICE = "app/dispatch/service.py"

LINK_OPERATIONS = {"upload_mission", "start_mission", "pause_mission", "return_to_home", "land"}
# Distinctive names: calling these on anything outside the service is always a violation.
LINK_ONLY_NAMES = {"upload_mission", "start_mission", "pause_mission"}
# `return_to_home` and `land` are also the names of the service's own methods, which the API
# calls legitimately, so they only count when the receiver is a drone link.
LINK_RECEIVER_HINT = "link"

LINK_IMPORTERS = {
    "app/main.py",  # the composition root builds one and hands it to the service
    "app/worker.py",  # the worker builds a DisabledDroneLink: a link that refuses everything
}
LINK_MODULES = {"app.dispatch.link", "app.dispatch.in_memory_link"}

STEERING_NAMES = (
    "goto",
    "go_to",
    "fly_to",
    "move_to",
    "set_position",
    "set_velocity",
    "set_heading",
    "set_altitude",
    "set_attitude",
    "set_speed",
    "set_throttle",
    "steer",
    "joystick",
    "manual_control",
    "rc_override",
    "override_rc",
    "send_setpoint",
    "setpoint",
    "offboard",
    "guided_target",
    "velocity_command",
    "yaw_rate",
)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def app_modules() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _receiver_text(node: ast.expr) -> str:
    return ast.unparse(node).lower()


# =============================================================================================
# The port itself
# =============================================================================================


class TestThePortCannotCarryASteeringCommand:
    def test_it_has_exactly_the_five_operations(self) -> None:
        names = {name for name in vars(DroneLink) if not name.startswith("_")}
        assert names == LINK_OPERATIONS

    @pytest.mark.parametrize("operation", sorted(LINK_OPERATIONS))
    def test_its_parameters_are_only_ids_a_mission_and_the_start_authorisation(
        self, operation: str
    ) -> None:
        parameters = set(inspect.signature(getattr(DroneLink, operation)).parameters) - {"self"}

        assert parameters <= {"drone_id", "mission", "authorization"}

    @pytest.mark.parametrize("operation", sorted(LINK_OPERATIONS))
    def test_no_operation_accepts_a_free_form_payload(self, operation: str) -> None:
        hints = typing.get_type_hints(getattr(DroneLink, operation))
        hints.pop("return", None)

        for name, hint in hints.items():
            assert hint in {Mission, StartAuthorization} or hint.__name__ == "UUID", (
                f"{operation}({name}: {hint}) could carry something other than an id, "
                "a mission or an authorisation"
            )

    def test_starting_needs_the_rule_2_authorisation(self) -> None:
        hints = typing.get_type_hints(DroneLink.start_mission)

        assert hints["authorization"] is StartAuthorization

    def test_uploading_returns_an_acknowledgement_to_verify(self) -> None:
        assert typing.get_type_hints(DroneLink.upload_mission)["return"] is MissionAck

    def test_the_in_memory_link_has_no_public_operation_beyond_the_port_and_test_controls(
        self,
    ) -> None:
        public = {n for n in vars(InMemoryDroneLink) if not n.startswith("_")}

        assert public - LINK_OPERATIONS == {
            "set_failure",
            "corrupt_next_ack",
            "operations",
        }


# =============================================================================================
# Who may talk to a drone
# =============================================================================================


def find_link_calls(source: str) -> list[str]:
    """Every use of a drone-link operation in ``source`` (calls and bare references)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Attribute) and node.attr in LINK_OPERATIONS):
            continue
        if node.attr in LINK_ONLY_NAMES or LINK_RECEIVER_HINT in _receiver_text(node.value):
            found.append(f"line {node.lineno}: {node.attr}")
    return found


def imports_link(source: str) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in LINK_MODULES:
                modules.append(node.module)
            elif node.module == "app.dispatch":
                modules.extend(
                    f"app.dispatch.{a.name}"
                    for a in node.names
                    if f"app.dispatch.{a.name}" in LINK_MODULES
                )
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names if a.name in LINK_MODULES)
    return modules


NOT_THE_SERVICE = [p for p in app_modules() if _relative(p) != SERVICE]
NOT_DISPATCH_OR_ROOT = [
    p
    for p in app_modules()
    if not (_relative(p).startswith("app/dispatch/") or _relative(p) in LINK_IMPORTERS)
]


@pytest.mark.parametrize("path", NOT_THE_SERVICE, ids=_relative)
def test_only_the_dispatch_service_uses_a_link_operation(path: Path) -> None:
    assert find_link_calls(path.read_text()) == []


@pytest.mark.parametrize("path", NOT_DISPATCH_OR_ROOT, ids=_relative)
def test_only_dispatch_and_the_composition_root_import_the_link(path: Path) -> None:
    assert imports_link(path.read_text()) == []


def test_the_scans_cover_most_of_the_code_base() -> None:
    assert len(NOT_THE_SERVICE) >= 50
    assert len(NOT_DISPATCH_OR_ROOT) >= 40
    assert ROOT / "app/api/internal/flights.py" in NOT_DISPATCH_OR_ROOT


def test_the_scan_sees_the_dispatch_service_using_the_link() -> None:
    """Guards the checks above against matching nothing because of a wrong path."""
    used = find_link_calls((ROOT / SERVICE).read_text())
    assert {u.split(": ")[1] for u in used} == LINK_OPERATIONS


def test_the_composition_root_imports_the_link_but_never_calls_it() -> None:
    source = (ROOT / "app/main.py").read_text()

    assert imports_link(source) != []
    assert find_link_calls(source) == []


# =============================================================================================
# Nothing in the code base is a steering primitive
# =============================================================================================


def find_steering_names(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        name: str | None = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.arg):
            name = node.arg
        if name is not None and any(word in name.lower() for word in STEERING_NAMES):
            found.append(f"line {getattr(node, 'lineno', '?')}: {name}")
    return found


@pytest.mark.parametrize("path", app_modules(), ids=_relative)
def test_nothing_is_named_like_a_steering_primitive(path: Path) -> None:
    assert find_steering_names(path.read_text()) == []


# =============================================================================================
# Safety is never gated; a start is only sent after its gated record (Rule 2)
# =============================================================================================

SAFETY_FUNCTIONS = {
    "pause_flight",
    "return_to_home",
    "land",
    "_safety_command",
    "_end_for_cancellation",
}
GATE_NAMES = {
    "_compliance",
    "validate_permission",
    "request_permission",
    "PermissionExpiredError",
    "PermissionRevokedError",
    "PermissionCheckStaleError",
    "PermissionWindowTooShortError",
}


def _functions(source: str) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }


def names_used(node: ast.AST) -> set[str]:
    used: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


def gated_safety_functions(source: str) -> list[str]:
    """Safety functions in ``source`` that touch the compliance provider or its errors."""
    functions = _functions(source)
    return sorted(
        f"{name} uses {sorted(names_used(functions[name]) & GATE_NAMES)}"
        for name in SAFETY_FUNCTIONS & set(functions)
        if names_used(functions[name]) & GATE_NAMES
    )


def start_sent_before_the_gate(source: str) -> list[str]:
    """Problems with how ``_start`` orders the gated record and the start command."""
    start = _functions(source).get("_start")
    if start is None:
        return ["no _start function"]
    gated = [
        n.lineno
        for n in ast.walk(start)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "transition"
        and "_flights" in _receiver_text(n.func.value)
    ]
    sent = [
        n.lineno
        for n in ast.walk(start)
        if isinstance(n, ast.Attribute) and n.attr == "start_mission"
    ]
    if len(gated) != 1:
        return [f"expected one gated transition in _start, found {len(gated)}"]
    if len(sent) != 1:
        return [f"expected one start command in _start, found {len(sent)}"]
    return [] if gated[0] < sent[0] else ["the start command precedes the gated transition"]


def test_safety_commands_never_consult_the_compliance_provider() -> None:
    source = (ROOT / SERVICE).read_text()

    assert set(_functions(source)) >= SAFETY_FUNCTIONS, "the scan must find the functions"
    assert gated_safety_functions(source) == []


def test_start_is_sent_only_after_the_gated_transition_is_recorded() -> None:
    assert start_sent_before_the_gate((ROOT / SERVICE).read_text()) == []


def test_the_start_command_is_sent_from_exactly_one_place() -> None:
    sources = {_relative(p): p.read_text() for p in app_modules()}
    uses = {
        name: [u for u in find_link_calls(src) if u.endswith("start_mission")]
        for name, src in sources.items()
    }

    assert {name: len(found) for name, found in uses.items() if found} == {SERVICE: 1}


def test_dispatch_never_calls_the_link_directly_it_goes_through_start() -> None:
    """``_run_workflow`` starts a flight only via ``_start`` (so the gate always runs)."""
    run = _functions((ROOT / SERVICE).read_text())["_run_workflow"]

    assert "start_mission" not in names_used(run)
    assert "_start" in names_used(run)


# =============================================================================================
# The detectors themselves must be able to fail
# =============================================================================================


class TestTheDetectorsThemselves:
    @pytest.mark.parametrize(
        "source",
        [
            "await self._link.upload_mission(d, m)",
            "await link.start_mission(d, a)",
            "await drone_link.land(d)",
            "await app.state.drone_link.return_to_home(d)",
            "x = self._link.pause_mission",  # a bare reference counts
            "await anything.upload_mission(d, m)",  # distinctive names count on any receiver
        ],
    )
    def test_flags_use_of_a_link_operation(self, source: str) -> None:
        assert find_link_calls(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "await service.return_to_home(flight_id)",  # the service's own method
            "await service.land(flight_id, source=s)",
            "await dispatch.pause_flight(flight_id)",
        ],
    )
    def test_allows_the_services_own_commands(self, source: str) -> None:
        assert find_link_calls(source) == []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.dispatch.link import DroneLink",
            "from app.dispatch.in_memory_link import InMemoryDroneLink",
            "import app.dispatch.link",
            "from app.dispatch import link",
        ],
    )
    def test_flags_importing_the_link(self, source: str) -> None:
        assert imports_link(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "async def goto(self, lat, lon): ...",
            "def set_velocity(vx, vy, vz): ...",
            "class JoystickController: ...",
            "drone.set_heading(90)",
            "def f(throttle_setpoint): ...",
            "def rc_override(channels): ...",
        ],
    )
    def test_flags_steering_names(self, source: str) -> None:
        assert find_steering_names(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "async def pause_flight(self, flight_id): ...",
            "def plan_mission(order, drone): ...",
            "async def return_to_home(self): ...",
        ],
    )
    def test_allows_high_level_names(self, source: str) -> None:
        assert find_steering_names(source) == []

    def test_flags_a_safety_command_that_consults_the_compliance_provider(self) -> None:
        source = (
            "class S:\n"
            "    async def land(self, flight_id):\n"
            "        await self._compliance.validate_permission(p)\n"
            "        await self._safety_command('land', f, g)\n"
        )
        assert gated_safety_functions(source) != []

    def test_flags_a_safety_command_that_raises_a_permission_error(self) -> None:
        source = (
            "class S:\n"
            "    async def return_to_home(self, flight_id):\n"
            "        raise PermissionRevokedError('nope')\n"
        )
        assert gated_safety_functions(source) != []

    def test_flags_a_start_sent_before_the_gated_transition(self) -> None:
        source = (
            "class S:\n"
            "    async def _start(self, f):\n"
            "        await self._link.start_mission(d, a)\n"
            "        self._flights.transition(f, STARTED)\n"
        )
        assert start_sent_before_the_gate(source) == [
            "the start command precedes the gated transition"
        ]

    def test_flags_a_start_with_no_gated_transition(self) -> None:
        source = (
            "class S:\n"
            "    async def _start(self, f):\n"
            "        await self._link.start_mission(d, a)\n"
        )
        assert start_sent_before_the_gate(source) != []

    def test_accepts_the_right_order(self) -> None:
        source = (
            "class S:\n"
            "    async def _start(self, f):\n"
            "        self._flights.transition(f, STARTED)\n"
            "        await self._link.start_mission(d, a)\n"
        )
        assert start_sent_before_the_gate(source) == []


# =============================================================================================
# MQTT: who may publish a command, who may touch a store (Rules 1 and 4)
# =============================================================================================

COMMAND_TOPIC_NAMES = {"command_topic", "parse_command_topic"}
COMMAND_TOPIC_HOMES = {
    "app/protocol/topics.py",  # defines them
    "app/dispatch/mqtt_link.py",  # the one publisher of commands
}
COMMAND_TOPIC_PREFIXES = (
    "app/simulator/",  # the drone side subscribes to them
    "app/demo/",  # the demo's loopback plays the drone side (it receives, never sends, commands)
)
PUBLISHERS = {
    "app/dispatch/mqtt_link.py",  # commands
    "app/mqtt/client.py",  # the client's own publish
    "app/simulator/runner.py",  # the drone's acknowledgements and events
}
COMMAND_CLASSES = {"UploadMission", "StartMission", "PauseMission", "ReturnToHome", "Land"}
COMMAND_BUILDERS = {"app/dispatch/mqtt_link.py"}
COMMAND_BUILDER_PREFIXES = ("app/simulator/",)  # only pattern-matches on them, but be lenient
STORE_NAMES = {"orders", "flights", "drones", "audit", "uow", "UnitOfWork", "SqlAlchemyUnitOfWork"}


def references_command_topics(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        name = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else None
        )
        if name in COMMAND_TOPIC_NAMES:
            found.append(f"line {node.lineno}: {name}")
    return found


def publishes(source: str) -> list[str]:
    """Publishes through something that looks like an MQTT client or the MQTT port.

    Redis broadcasts (``self._broadcast.publish``) are views for people, not messages to
    drones, and are deliberately not flagged: the only way to hold an MQTT client is to import
    one, which the layer rules confine to ``app/mqtt``, ``app/dispatch/mqtt_link.py``, the
    simulator and the composition roots.
    """
    return [
        f"line {node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr == "publish"
        and any(hint in _receiver_text(node.value) for hint in ("mqtt", "client"))
    ]


def builds_commands(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name in COMMAND_CLASSES:
            found.append(f"line {node.lineno}: {name}(...)")
    return found


def touches_stores(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        name = (
            node.id
            if isinstance(node, ast.Name)
            else node.attr
            if isinstance(node, ast.Attribute)
            else node.name
            if isinstance(node, ast.alias)
            else None
        )
        if name is not None and (name.lstrip("_") in STORE_NAMES or "uow" in name.lower()):
            found.append(f"line {getattr(node, 'lineno', '?')}: {name}")
    return found


# The ONLY methods of the dispatch service each telemetry module may call. Anything else on
# ``self._dispatch`` (a private route to the database, a command, a cancellation) is a violation.
ALLOWED_SERVICE_CALLS = {
    "app/telemetry/ingest.py": {"record_progress"},
    "app/telemetry/recorder.py": {"drone_exists", "record_battery"},
    "app/telemetry/watchdog.py": {"list_live_flights"},
    "app/telemetry/reaper.py": {"release_stale_reservations"},
}


def reaches_into_the_service(source: str, allowed: set[str] | None = None) -> list[str]:
    """Anything on ``self._dispatch`` other than the ``allowed`` methods (private or public)."""
    permitted = allowed if allowed is not None else {"record_progress"}
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_dispatch"
            and node.attr not in permitted
        ):
            found.append(f"line {node.lineno}: _dispatch.{node.attr}")
    return found


def _allowed(path: Path, homes: set[str], prefixes: tuple[str, ...] = ()) -> bool:
    relative = _relative(path)
    return relative in homes or relative.startswith(prefixes)


@pytest.mark.parametrize(
    "path",
    [p for p in app_modules() if not _allowed(p, COMMAND_TOPIC_HOMES, COMMAND_TOPIC_PREFIXES)],
    ids=_relative,
)
def test_only_the_mqtt_link_refers_to_the_command_topics(path: Path) -> None:
    assert references_command_topics(path.read_text()) == []


@pytest.mark.parametrize(
    "path", [p for p in app_modules() if _relative(p) not in PUBLISHERS], ids=_relative
)
def test_only_the_mqtt_link_the_client_and_the_simulator_publish(path: Path) -> None:
    assert publishes(path.read_text()) == []


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in app_modules()
        if _relative(p) not in COMMAND_BUILDERS
        and _relative(p) != "app/protocol/messages.py"
        and not _relative(p).startswith(COMMAND_BUILDER_PREFIXES)
    ],
    ids=_relative,
)
def test_only_the_mqtt_link_builds_a_command(path: Path) -> None:
    assert builds_commands(path.read_text()) == []


TELEMETRY_MODULES = [p for p in app_modules() if _relative(p).startswith("app/telemetry/")]


@pytest.mark.parametrize("path", TELEMETRY_MODULES, ids=_relative)
def test_the_telemetry_ingest_has_no_store_and_cannot_publish(path: Path) -> None:
    """Rule 4: it writes nothing itself. Rule 1: it has no client to send a command with."""
    source = path.read_text()

    assert touches_stores(source) == []
    assert reaches_into_the_service(source, ALLOWED_SERVICE_CALLS.get(_relative(path), set())) == []
    assert publishes(source) == []
    assert references_command_topics(source) == []
    assert builds_commands(source) == []


def test_the_worker_hands_its_ingest_a_service_that_cannot_command_a_drone() -> None:
    source = (ROOT / "app/worker.py").read_text()

    assert "DisabledDroneLink()" in source
    assert "DisabledCompliance()" in source
    assert find_link_calls(source) == []
    assert publishes(source) == []


def test_the_telemetry_scan_sees_every_module_that_reads_the_service() -> None:
    scanned = {_relative(p) for p in TELEMETRY_MODULES}

    assert set(ALLOWED_SERVICE_CALLS) <= scanned
    assert len(TELEMETRY_MODULES) >= 6


def test_each_telemetry_module_really_calls_only_what_it_is_allowed_to() -> None:
    """Guards the allow-list against going stale: every entry is actually used."""
    for relative, allowed in ALLOWED_SERVICE_CALLS.items():
        source = (ROOT / relative).read_text()
        used = {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_dispatch"
        }
        assert used == allowed, relative


class TestTheMqttDetectorsThemselves:
    @pytest.mark.parametrize(
        "source",
        [
            "t = command_topic(drone_id)",
            "t = topics.command_topic(drone_id)",
            "f = parse_command_topic",
        ],
    )
    def test_flags_references_to_command_topics(self, source: str) -> None:
        assert references_command_topics(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "await self._broadcast.publish(channel, envelope)",
            "await self._redis.publish(channel, payload)",
            "await self.publish(channel, envelope)",
        ],
    )
    def test_allows_redis_broadcasts_which_are_views_not_drone_messages(self, source: str) -> None:
        assert publishes(source) == []

    def test_allows_event_and_ack_topics(self) -> None:
        assert references_command_topics("topics.event_topic(d); topics.ack_topic(i)") == []

    @pytest.mark.parametrize(
        "source", ["await client.publish(t, p)", "await self._mqtt.publish(t)"]
    )
    def test_flags_publishing(self, source: str) -> None:
        assert publishes(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "wire.Land(**envelope)",
            "UploadMission(mission=m)",
            "messages.ReturnToHome()",
            "PauseMission()",
            "StartMission(authorization=a)",
        ],
    )
    def test_flags_building_a_command(self, source: str) -> None:
        assert builds_commands(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "uow.orders.get(i)",
            "self._store.flights",
            "from app.services.ports import UnitOfWork",
            "x = drones",
            "async with self._uow_factory() as uow: pass",
        ],
    )
    def test_flags_touching_a_store(self, source: str) -> None:
        assert touches_stores(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "self._dispatch._uow_factory()",  # a private route to the database
            "self._dispatch._orders.transition(a, b)",
            "x = self._dispatch._flights",
            "self._dispatch.cancel_order(i)",  # a public method it has no business calling
            "await self._dispatch.return_to_home(f)",
        ],
    )
    def test_flags_reaching_past_record_progress(self, source: str) -> None:
        assert touches_stores(source) != [] or reaches_into_the_service(source) != []

    def test_the_ingest_may_call_record_progress_and_nothing_else(self) -> None:
        assert reaches_into_the_service("await self._dispatch.record_progress(a, b, c)") == []

    def test_a_module_may_call_its_own_allowed_methods_but_not_the_ingests(self) -> None:
        source = "await self._dispatch.record_battery(a, b)"

        assert reaches_into_the_service(source, {"record_battery"}) == []
        assert reaches_into_the_service(source) != []

    def test_the_recorder_may_not_cancel_or_command(self) -> None:
        allowed = ALLOWED_SERVICE_CALLS["app/telemetry/recorder.py"]

        assert reaches_into_the_service("await self._dispatch.cancel_order(i)", allowed) != []
        assert reaches_into_the_service("await self._dispatch._uow_factory()", allowed) != []

    def test_allows_a_module_that_only_calls_the_service(self) -> None:
        assert touches_stores("result = await self._dispatch.record_progress(a, b, c)") == []
