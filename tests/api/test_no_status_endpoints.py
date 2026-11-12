"""Rule 4 at the API level: no endpoint can set an order's status.

Orders are created through an adapter and move only through the state machine, so the
API must expose no way to write a status. This inspects the app's public OpenAPI document
(the one place that lists every operation, including those on included routers) plus its
plain Starlette routes, instead of trusting a convention.

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

EXPECTED_OPERATIONS = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/partner/{client_id}/orders"),
    ("GET", "/partner/{client_id}/orders/{external_order_id}"),
    ("GET", "/internal/orders/{order_id}"),
    ("GET", "/internal/orders/{order_id}/status"),
}


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
        if any("status" in name.lower() for name in input_names(schema, operation)):
            problems.append(f"{method} {path}: accepts a field named like 'status'")
    return problems


# --- the real app ---------------------------------------------------------------------


def test_the_operations_are_exactly_the_expected_ones(app: FastAPI) -> None:
    assert set(operations(app.openapi())) == EXPECTED_OPERATIONS


def test_the_only_write_operation_creates_an_order_through_a_partner_adapter(
    app: FastAPI,
) -> None:
    writes = {key for key in operations(app.openapi()) if key[0] not in READ_ONLY}
    assert writes == {("POST", "/partner/{client_id}/orders")}


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


def test_the_inspection_flags_a_delete() -> None:
    assert any(p.startswith("DELETE ") for p in status_writing_operations(_bad_app()))


def test_the_inspection_sees_the_operations_of_included_routers(app: FastAPI) -> None:
    """The very thing that made the first version of this test see nothing."""
    assert len(operations(app.openapi())) >= 6
