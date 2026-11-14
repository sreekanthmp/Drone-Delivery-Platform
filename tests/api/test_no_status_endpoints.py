"""Rules 1 and 4 at the API level: no endpoint can set a status or steer a drone.

Orders are created through an adapter and move only through the state machine, so the
API must expose no way to write a status (of an order, a flight or a drone). And the cloud
never steers a drone: the only flight commands are start, pause, resume, return-to-home and
land, which take nothing but the flight's id and an optional reason. This inspects the app's
public OpenAPI document (the one place that lists every operation, including those on
included routers) plus its plain Starlette routes, instead of trusting a convention.

The inspection helpers are themselves tested against a deliberately bad app, so a pass
here cannot be an inspection that sees nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from starlette.routing import Route

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
READ_ONLY = {"GET", "HEAD", "OPTIONS"}

DRONE_FIELDS = {
    "name",
    "home",
    "latitude",
    "longitude",
    "max_payload_kg",
    "max_range_km",
    "battery_percent",
}

# Every operation that changes anything, with the EXACT inputs it may accept. Adding an
# endpoint, or an input to one, means changing this table on purpose.
WRITE_OPERATIONS: dict[tuple[str, str], set[str]] = {
    # a partner's order payload is opaque to OpenAPI: the adapter parses it (Rule 3)
    ("POST", "/partner/{client_id}/orders"): {"client_id"},
    ("POST", "/partner/{client_id}/orders/{external_order_id}/cancel"): {
        "client_id",
        "external_order_id",
    },
    ("POST", "/internal/orders/{order_id}/dispatch"): {"order_id", "start"},
    ("POST", "/internal/drones"): DRONE_FIELDS,
    ("POST", "/internal/drones/{drone_id}/docked"): {"drone_id"},
    ("POST", "/internal/drones/{drone_id}/maintenance"): {"drone_id"},
    ("POST", "/internal/flights/{flight_id}/start"): {"flight_id"},
    ("POST", "/internal/flights/{flight_id}/pause"): {"flight_id"},
    ("POST", "/internal/flights/{flight_id}/resume"): {"flight_id"},
    ("POST", "/internal/flights/{flight_id}/return-to-home"): {"flight_id", "reason"},
    ("POST", "/internal/flights/{flight_id}/land"): {"flight_id", "reason"},
    # gives a FAILED webhook delivery another round of attempts; touches no order and no drone
    ("POST", "/internal/webhooks/deliveries/{event_id}/retry"): {"event_id"},
}

READ_OPERATIONS = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/partner/{client_id}/orders/{external_order_id}"),
    ("GET", "/internal/orders/{order_id}"),
    ("GET", "/internal/orders/{order_id}/status"),
    ("GET", "/internal/orders/{order_id}/flights"),
    ("GET", "/internal/drones"),
    ("GET", "/internal/drones/{drone_id}"),
    ("GET", "/internal/flights/{flight_id}"),
    ("GET", "/internal/alerts"),
    ("GET", "/internal/webhooks/deliveries"),
    ("GET", "/internal/webhooks/deliveries/{event_id}"),
    ("GET", "/internal/orders/{order_id}/webhooks"),
}

EXPECTED_OPERATIONS = READ_OPERATIONS | set(WRITE_OPERATIONS)

# Words that would mean a client could steer a drone, rather than command it at a high level.
STEERING_WORDS = (
    "lat",
    "lon",
    "lng",
    "alt",
    "heading",
    "yaw",
    "pitch",
    "roll",
    "throttle",
    "velocity",
    "speed",
    "waypoint",
    "goto",
    "position",
    "joystick",
    "steer",
    "target",
    "route",
    "mission",
    "payload",
)


def operations(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): operation
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def _resolve(schema: dict[str, Any], node: Any) -> Any:
    if isinstance(node, dict) and "$ref" in node:
        target: Any = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return _resolve(schema, target)
    return node


def _property_names(
    schema: dict[str, Any], node: Any, seen: frozenset[str] = frozenset()
) -> set[str]:
    node = _resolve(schema, node)
    names: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                names |= set(value)
            ref = value.get("$ref") if isinstance(value, dict) else None
            if ref in seen:
                continue
            names |= _property_names(schema, value, seen | ({ref} if ref else set()))
    elif isinstance(node, list):
        for item in node:
            names |= _property_names(schema, item, seen)
    return names


def input_names(schema: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """Every parameter name and every request-body property an operation accepts."""
    names = {parameter["name"] for parameter in operation.get("parameters", [])}
    if "requestBody" in operation:
        names |= _property_names(schema, operation["requestBody"])
    return names


def status_writing_operations(app: FastAPI) -> list[str]:
    """Descriptions of every operation that could write a status (should be none)."""
    schema = app.openapi()
    problems: list[str] = []
    for (method, path), operation in operations(schema).items():
        if method in {"PUT", "PATCH", "DELETE"}:
            problems.append(f"{method} {path}: forbidden method")
        # A read cannot write a status, so a filter such as ``GET ...?status=failed`` is fine;
        # anything that can change something must not accept one.
        if method not in READ_ONLY and any(
            "status" in name.lower() for name in input_names(schema, operation)
        ):
            problems.append(f"{method} {path}: accepts a field named like 'status'")
    return problems


def steering_inputs(app: FastAPI) -> list[str]:
    """Inputs of the flight and dispatch operations whose name suggests steering."""
    schema = app.openapi()
    problems: list[str] = []
    for (method, path), operation in operations(schema).items():
        if not (path.startswith("/internal/flights/") or path.endswith("/dispatch")):
            continue
        for name in sorted(input_names(schema, operation)):
            if any(word in name.lower() for word in STEERING_WORDS):
                problems.append(f"{method} {path}: input '{name}'")
    return problems


# --- the real app ---------------------------------------------------------------------


def test_the_operations_are_exactly_the_expected_ones(app: FastAPI) -> None:
    assert set(operations(app.openapi())) == EXPECTED_OPERATIONS


def test_the_write_operations_are_exactly_the_expected_ones(app: FastAPI) -> None:
    writes = {key for key in operations(app.openapi()) if key[0] not in READ_ONLY}
    assert writes == set(WRITE_OPERATIONS)


def test_every_write_accepts_exactly_the_inputs_it_is_allowed(app: FastAPI) -> None:
    schema = app.openapi()
    found = {
        key: input_names(schema, operation)
        for key, operation in operations(schema).items()
        if key in WRITE_OPERATIONS
    }
    assert found == WRITE_OPERATIONS


def test_orders_are_created_only_through_a_partner_adapter(app: FastAPI) -> None:
    """No internal route creates an order: partner payloads must pass through an adapter."""
    creators = [
        (method, path)
        for (method, path) in operations(app.openapi())
        if method == "POST" and path.startswith("/internal/") and path.endswith("/orders")
    ]
    assert creators == []


def test_flight_commands_take_only_the_flight_id_and_a_reason(app: FastAPI) -> None:
    """Rule 1: start, pause, resume, return-to-home, land. Nothing carries a position."""
    schema = app.openapi()
    commands = {
        key: input_names(schema, operation)
        for key, operation in operations(schema).items()
        if key[0] == "POST" and key[1].startswith("/internal/flights/")
    }
    assert len(commands) == 5
    for names in commands.values():
        assert names <= {"flight_id", "reason"}


def test_no_flight_or_dispatch_input_could_steer_a_drone(app: FastAPI) -> None:
    assert steering_inputs(app) == []


def test_no_operation_can_write_a_status(app: FastAPI) -> None:
    assert status_writing_operations(app) == []


def test_the_plain_routes_outside_openapi_are_read_only(app: FastAPI) -> None:
    plain = [route for route in app.routes if isinstance(route, Route)]
    assert plain, "the docs/openapi routes should be visible here"
    for route in plain:
        assert (route.methods or set()) <= READ_ONLY, route.path


def test_openapi_generation_works(app: FastAPI) -> None:
    assert app.openapi()["info"]["title"] == "Drone Delivery Platform"


# --- the inspection itself must be able to fail --------------------------------------


class _StatusBody(BaseModel):
    status: str


def _bad_app() -> FastAPI:
    bad = FastAPI()

    @bad.put("/orders/{order_id}")
    async def put_order(order_id: str) -> None: ...

    @bad.patch("/orders/{order_id}/status")
    async def patch_status(order_id: str, body: _StatusBody) -> None: ...

    @bad.post("/orders/{order_id}/transition")
    async def transition(order_id: str, status: str) -> None: ...

    @bad.delete("/orders/{order_id}")
    async def delete_order(order_id: str) -> None: ...

    return bad


def test_the_inspection_flags_a_put_endpoint() -> None:
    assert any(
        p.startswith("PUT /orders/{order_id}") for p in status_writing_operations(_bad_app())
    )


def test_the_inspection_flags_a_patch_with_a_status_body() -> None:
    problems = status_writing_operations(_bad_app())
    assert any("PATCH" in p and "forbidden method" in p for p in problems)
    assert any("PATCH" in p and "'status'" in p for p in problems), "body fields are resolved"


def test_the_inspection_flags_a_status_query_parameter() -> None:
    problems = status_writing_operations(_bad_app())
    assert any(p.startswith("POST /orders/{order_id}/transition") for p in problems)


def test_a_status_filter_on_a_read_is_not_a_status_write() -> None:
    """``GET /internal/webhooks/deliveries?status=failed`` lists; it cannot write anything."""
    ok = FastAPI()

    @ok.get("/deliveries")
    async def list_deliveries(status: str | None = None) -> None: ...

    @ok.head("/deliveries")
    async def head_deliveries(status: str | None = None) -> None: ...

    assert status_writing_operations(ok) == []


def test_the_status_filter_is_still_refused_on_anything_that_writes() -> None:
    bad = FastAPI()

    @bad.post("/deliveries")
    async def create(status: str) -> None: ...

    @bad.post("/deliveries/{delivery_id}/retry")
    async def retry(delivery_id: str, body: _StatusBody) -> None: ...

    problems = status_writing_operations(bad)
    assert len(problems) == 2
    assert all("'status'" in p for p in problems)


class _Goto(BaseModel):
    latitude: float
    longitude: float
    altitude_m: float


def test_the_steering_check_flags_a_flight_command_that_takes_a_position() -> None:
    bad = FastAPI()

    @bad.post("/internal/flights/{flight_id}/goto")
    async def goto(flight_id: str, body: _Goto) -> None: ...

    @bad.post("/internal/flights/{flight_id}/set-speed")
    async def set_speed(flight_id: str, speed: float) -> None: ...

    problems = steering_inputs(bad)
    assert any("'latitude'" in p for p in problems)
    assert any("'altitude_m'" in p for p in problems)
    assert any("'speed'" in p for p in problems)


def test_the_steering_check_does_not_flag_the_real_commands(app: FastAPI) -> None:
    schema = app.openapi()
    checked = [
        path
        for (_, path) in operations(schema)
        if path.startswith("/internal/flights/") or path.endswith("/dispatch")
    ]
    # the flight read, the five flight commands and the dispatch
    assert len(checked) == 7, "the check must actually look at the flight commands"


def test_the_inspection_flags_a_delete() -> None:
    assert any(p.startswith("DELETE ") for p in status_writing_operations(_bad_app()))


def test_the_inspection_sees_the_operations_of_included_routers(app: FastAPI) -> None:
    """The very thing that made the first version of this test see nothing."""
    assert len(operations(app.openapi())) >= len(EXPECTED_OPERATIONS)
