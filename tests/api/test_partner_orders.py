"""Partner gateway: creating and looking up orders through each client's adapter (Rule 3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.config import Settings
from app.domain.models import AuditEventType, EventSource, OrderStatus
from app.main import create_app
from app.services.order_service import OrderService
from tests.api.conftest import (
    CLIENTS,
    PARTNER_STATUS,
    PLATFORM_REF,
    create_path,
    lookup_path,
)
from tests.fakes import InMemoryDatabase
from tests.payloads import EXTERNAL_IDS, VALID_PAYLOADS, drop_path, set_path

S = OrderStatus

# Each partner reports an error's kind and the offending field paths in its own shape.
ERROR_KIND: dict[str, Callable[[dict[str, Any]], str]] = {
    "mock": lambda b: b["error"]["code"],
    "swiggy": lambda b: b["error_code"].lower(),
    "zomato": lambda b: b["error"]["type"],
}
ERROR_PATHS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "mock": lambda b: [d["field"] for d in b["error"]["details"]],
    "swiggy": lambda b: [e["field"] for e in b["errors"]],
    "zomato": lambda b: [f["path"] for f in b["error"]["fields"]],
}
# After created -> assigned, what each partner calls the new status.
ASSIGNED_NAME = {"mock": "assigned", "swiggy": "DRONE_ASSIGNED", "zomato": "dispatch_assigned"}
CREATED_NAME = {"mock": "created", "swiggy": "PLACED", "zomato": "accepted"}


def same_pickup_and_dropoff(client_id: str) -> dict[str, Any]:
    payload = VALID_PAYLOADS[client_id]()
    if client_id == "mock":
        return set_path(payload, "dropoff", payload["pickup"])
    if client_id == "swiggy":
        return set_path(payload, "customer.address", payload["restaurant"]["location"])
    return set_path(payload, "order.drop_point", payload["order"]["pickup_point"])


def bad_latitude(client_id: str) -> tuple[dict[str, Any], str]:
    payload = VALID_PAYLOADS[client_id]()
    if client_id == "mock":
        return set_path(payload, "pickup.lat", 95), "pickup.lat"
    if client_id == "swiggy":
        return (
            set_path(payload, "restaurant.location.latitude", 95),
            "restaurant.location.latitude",
        )
    return (
        set_path(payload, "order.pickup_point.coordinates.1", 95),
        "order.pickup_point.coordinates[1]",
    )


@pytest.mark.parametrize("client_id", CLIENTS)
class TestCreateOrder:
    async def test_a_new_order_is_201_in_the_partners_own_format(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        response = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())

        assert response.status_code == 201
        assert response.headers["content-type"] == "application/json"
        body = response.json()
        assert PARTNER_STATUS[client_id](body) == CREATED_NAME[client_id]
        UUID(PLATFORM_REF[client_id](body))  # a valid tracking reference

    async def test_the_order_is_stored_in_the_internal_model(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        response = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())

        (order,) = database.orders.values()
        assert str(order.id) == PLATFORM_REF[client_id](response.json())
        assert order.client_id == client_id
        assert order.external_order_id == EXTERNAL_IDS[client_id]
        assert order.status is S.CREATED
        assert order.drone_id is None
        assert order.pickup.latitude == pytest.approx(12.9716)
        assert order.pickup.longitude == pytest.approx(77.5946)
        assert order.dropoff.latitude == pytest.approx(12.9352)
        assert order.dropoff.longitude == pytest.approx(77.6245)

    async def test_creation_is_audited_with_the_adapter_as_source(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())

        (event,) = database.audit
        assert event.event_type is AuditEventType.ORDER_CREATED
        assert event.source is EventSource.ADAPTER
        assert event.new_state is S.CREATED
        assert dict(event.metadata) == {"client_id": client_id}

    async def test_a_repeat_is_200_with_the_same_order_and_no_new_audit_row(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        payload = VALID_PAYLOADS[client_id]()
        first = await client.post(create_path(client_id), json=payload)
        second = await client.post(create_path(client_id), json=payload)

        assert (first.status_code, second.status_code) == (201, 200)
        assert PLATFORM_REF[client_id](first.json()) == PLATFORM_REF[client_id](second.json())
        assert len(database.orders) == 1
        assert len(database.audit) == 1

    async def test_unknown_fields_are_ignored(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        payload = set_path(VALID_PAYLOADS[client_id](), "extra_field", {"a": 1})

        response = await client.post(create_path(client_id), json=payload)

        assert response.status_code == 201

    async def test_a_partner_cannot_choose_the_status(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        payload = set_path(VALID_PAYLOADS[client_id](), "status", "delivered")
        payload = set_path(payload, "order_status", "DELIVERED")

        response = await client.post(create_path(client_id), json=payload)

        assert response.status_code == 201
        (order,) = database.orders.values()
        assert order.status is S.CREATED
        assert PARTNER_STATUS[client_id](response.json()) == CREATED_NAME[client_id]


@pytest.mark.parametrize("client_id", CLIENTS)
class TestInvalidRequests:
    async def test_missing_fields_are_422_in_the_partners_error_format(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        response = await client.post(create_path(client_id), json={})

        assert response.status_code == 422
        body = response.json()
        assert ERROR_KIND[client_id](body) == "invalid_request"
        assert ERROR_PATHS[client_id](body), "field-level details are included"
        assert not database.orders
        assert not database.audit

    async def test_field_paths_point_at_the_problem(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        payload, expected_path = bad_latitude(client_id)

        response = await client.post(create_path(client_id), json=payload)

        assert response.status_code == 422
        assert expected_path in ERROR_PATHS[client_id](response.json())
        assert not database.orders

    async def test_a_required_field_removed_is_reported(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        required = {"mock": "package.weight_kg", "swiggy": "items", "zomato": "order.id"}[client_id]

        response = await client.post(
            create_path(client_id), json=drop_path(VALID_PAYLOADS[client_id](), required)
        )

        assert response.status_code == 422
        assert required in ERROR_PATHS[client_id](response.json())

    async def test_malformed_json_is_422_in_the_partners_format(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        response = await client.post(
            create_path(client_id),
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422
        body = response.json()
        assert ERROR_KIND[client_id](body) == "invalid_request"
        assert ERROR_PATHS[client_id](body) == ["$"]
        assert not database.orders

    @pytest.mark.parametrize("body", ["[]", '"text"', "42", "null", "true"])
    async def test_a_body_that_is_not_a_json_object_is_422(
        self, client: httpx.AsyncClient, client_id: str, body: str
    ) -> None:
        response = await client.post(
            create_path(client_id), content=body, headers={"content-type": "application/json"}
        )

        assert response.status_code == 422
        assert ERROR_PATHS[client_id](response.json()) == ["$"]

    async def test_an_empty_body_is_422(self, client: httpx.AsyncClient, client_id: str) -> None:
        response = await client.post(create_path(client_id))

        assert response.status_code == 422
        assert ERROR_KIND[client_id](response.json()) == "invalid_request"

    async def test_identical_pickup_and_dropoff_is_a_business_rule_failure_422(
        self, client: httpx.AsyncClient, database: InMemoryDatabase, client_id: str
    ) -> None:
        response = await client.post(
            create_path(client_id), json=same_pickup_and_dropoff(client_id)
        )

        assert response.status_code == 422
        assert ERROR_KIND[client_id](response.json()) == "invalid_request"
        assert not database.orders
        assert not database.audit


class TestIsolationBetweenPartners:
    async def test_the_same_external_id_from_two_partners_makes_two_orders(
        self, client: httpx.AsyncClient, database: InMemoryDatabase
    ) -> None:
        shared = "SHARED-1"
        mock = set_path(VALID_PAYLOADS["mock"](), "order_id", shared)
        swiggy = set_path(VALID_PAYLOADS["swiggy"](), "swiggy_order_id", shared)

        first = await client.post(create_path("mock"), json=mock)
        second = await client.post(create_path("swiggy"), json=swiggy)

        assert (first.status_code, second.status_code) == (201, 201)
        assert len(database.orders) == 2
        assert {o.client_id for o in database.orders.values()} == {"mock", "swiggy"}

    async def test_a_partner_cannot_see_another_partners_order(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.post(create_path("mock"), json=VALID_PAYLOADS["mock"]())

        # Same external id, asked for through a different partner's adapter.
        response = await client.get("/partner/swiggy/orders/ORD-1")

        assert response.status_code == 404
        assert ERROR_KIND["swiggy"](response.json()) == "not_found"


class TestUnknownClient:
    @pytest.mark.parametrize("client_id", ["nobody", "Swiggy", "SWIGGY", "swiggy.", "mock%20"])
    async def test_post_is_a_neutral_404(self, client: httpx.AsyncClient, client_id: str) -> None:
        response = await client.post(create_path(client_id), json={"anything": 1})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "unknown_client"

    async def test_get_is_a_neutral_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/partner/nobody/orders/1")

        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "unknown_client", "message": "unknown client 'nobody'"}
        }


@pytest.mark.parametrize("client_id", CLIENTS)
class TestLookup:
    async def test_lookup_by_the_partners_own_id_matches_the_create_response(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        created = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())

        found = await client.get(lookup_path(client_id))

        assert found.status_code == 200
        assert PLATFORM_REF[client_id](found.json()) == PLATFORM_REF[client_id](created.json())
        assert PARTNER_STATUS[client_id](found.json()) == CREATED_NAME[client_id]

    async def test_an_unknown_order_is_a_404_in_the_partners_format(
        self, client: httpx.AsyncClient, client_id: str
    ) -> None:
        response = await client.get(f"/partner/{client_id}/orders/does-not-exist")

        assert response.status_code == 404
        assert ERROR_KIND[client_id](response.json()) == "not_found"

    async def test_a_status_change_is_reported_in_the_partners_vocabulary(
        self, client: httpx.AsyncClient, service: OrderService, client_id: str
    ) -> None:
        created = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())
        order_id = UUID(PLATFORM_REF[client_id](created.json()))

        await service.transition(
            order_id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=UUID(int=0xD0D0)
        )

        found = await client.get(lookup_path(client_id))
        assert PARTNER_STATUS[client_id](found.json()) == ASSIGNED_NAME[client_id]

    async def test_the_drone_id_is_never_exposed_to_partners(
        self, client: httpx.AsyncClient, service: OrderService, client_id: str
    ) -> None:
        created = await client.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())
        order_id = UUID(PLATFORM_REF[client_id](created.json()))
        drone = UUID(int=0xD0D0)
        await service.transition(order_id, S.ASSIGNED, source=EventSource.DISPATCH, drone_id=drone)

        found = await client.get(lookup_path(client_id))

        assert str(drone) not in found.text


@pytest.mark.parametrize("client_id", CLIENTS)
class TestUnexpectedFailures:
    @staticmethod
    def _broken_app_client(client_id: str) -> httpx.AsyncClient:
        def explode() -> Any:
            raise RuntimeError("password=hunter2 host=db.internal")

        app = create_app(settings=Settings(_env_file=None), uow_factory=explode)  # type: ignore[call-arg]
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://platform.test",
        )

    async def test_create_returns_a_500_in_the_partners_format_without_internal_details(
        self, client_id: str
    ) -> None:
        async with self._broken_app_client(client_id) as broken:
            response = await broken.post(create_path(client_id), json=VALID_PAYLOADS[client_id]())

        assert response.status_code == 500
        assert ERROR_KIND[client_id](response.json()) == "internal_error"
        assert "hunter2" not in response.text
        assert "db.internal" not in response.text

    async def test_lookup_returns_a_500_in_the_partners_format(self, client_id: str) -> None:
        async with self._broken_app_client(client_id) as broken:
            response = await broken.get(lookup_path(client_id))

        assert response.status_code == 500
        assert ERROR_KIND[client_id](response.json()) == "internal_error"
        assert "hunter2" not in json.dumps(response.json())
