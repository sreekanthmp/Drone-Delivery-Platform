"""Contract every adapter must satisfy, checked across all registered adapters.

New adapters join automatically: they only need an entry in ``tests/payloads.VALID_PAYLOADS``
(and the registry), and these tests then apply to them.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest

from app.adapters.base import (
    AdapterValidationError,
    ClientAdapter,
    ClientError,
    ErrorCode,
    FieldIssue,
)
from app.adapters.registry import default_registry
from app.domain.models import OrderRequest, OrderStatus
from tests.payloads import EXTERNAL_IDS, VALID_PAYLOADS
from tests.unit.adapters.helpers import MakeOrder, assert_json_serialisable, event_for

ADAPTERS: list[ClientAdapter] = [
    default_registry().get(client_id) for client_id in default_registry().client_ids()
]
IDS = [adapter.client_id for adapter in ADAPTERS]

JUNK_PAYLOADS: list[Any] = [
    {},
    {"unrelated": 1},
    {"order": None},
    {"order": []},
    {"order": "x"},
    {"order": {"id": None}},
    {"order_id": None, "pickup": None, "dropoff": None, "package": None},
    {"items": None, "restaurant": [], "customer": 5},
    {"swiggy_order_id": {"nested": {"deeper": [1, 2, 3]}}},
    {"order": {"id": 10**40, "pickup_point": 3, "drop_point": [], "package_weight_grams": {}}},
    {"order": {"id": 1, "pickup_point": {"coordinates": "77,12"}}},
    {"": ""},
    {"a" * 10_000: "b" * 10_000},
    {
        "order_id": "x",
        "pickup": {"lat": [], "lng": {}},
        "dropoff": {},
        "package": {"weight_kg": []},
    },
]


def test_the_registry_contains_the_three_integrations() -> None:
    assert sorted(IDS) == ["mock", "swiggy", "zomato"]


def test_every_adapter_has_a_sample_payload_here() -> None:
    assert set(IDS) == set(VALID_PAYLOADS) == set(EXTERNAL_IDS)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
class TestEveryAdapter:
    def test_client_id_is_a_url_safe_lowercase_slug(self, adapter: ClientAdapter) -> None:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", adapter.client_id)

    def test_a_valid_payload_becomes_an_order_request_for_this_client(
        self, adapter: ClientAdapter
    ) -> None:
        request = adapter.parse_order(VALID_PAYLOADS[adapter.client_id]())

        assert isinstance(request, OrderRequest)
        assert request.client_id == adapter.client_id
        assert request.external_order_id == EXTERNAL_IDS[adapter.client_id]
        assert request.package.weight_kg > 0
        assert request.pickup != request.dropoff

    @pytest.mark.parametrize("junk", JUNK_PAYLOADS, ids=lambda j: repr(j)[:40])
    def test_junk_is_rejected_cleanly(self, adapter: ClientAdapter, junk: Any) -> None:
        """Whatever arrives, the only failure an adapter may raise is AdapterValidationError."""
        with pytest.raises(AdapterValidationError) as exc_info:
            adapter.parse_order(junk)

        issues = exc_info.value.issues
        assert issues, "a rejection must say what is wrong"
        assert all(issue.path and issue.message for issue in issues)

    def test_an_empty_payload_is_rejected_with_issues(self, adapter: ClientAdapter) -> None:
        with pytest.raises(AdapterValidationError) as exc_info:
            adapter.parse_order({})
        assert exc_info.value.issues

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_every_status_can_be_rendered_as_an_order_and_as_an_update(
        self, adapter: ClientAdapter, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        order = make_order(
            status, client_id=adapter.client_id, external_order_id=EXTERNAL_IDS[adapter.client_id]
        )

        for created in (True, False):
            response = adapter.render_order(order, created=created)
            assert response.status_code == (201 if created else 200)
            assert_json_serialisable(response.body)
        assert_json_serialisable(adapter.render_status_update(order, event_for(order, status)))

    def test_responses_never_leak_internal_only_data(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        drone = UUID(int=0xD0D0D0)
        order = make_order(
            OrderStatus.IN_FLIGHT,
            drone_id=drone,
            client_id=adapter.client_id,
            external_order_id=EXTERNAL_IDS[adapter.client_id],
        )

        rendered = [
            str(adapter.render_order(order, created=False).body),
            str(adapter.render_status_update(order, event_for(order, OrderStatus.IN_FLIGHT))),
        ]

        for text in rendered:
            assert str(drone) not in text, "the drone id is internal"
            assert "drone_id" not in text
            assert "package_weight_kg" not in text

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (ErrorCode.INVALID_REQUEST, 422),
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.INTERNAL_ERROR, 500),
        ],
    )
    def test_errors_use_the_expected_http_status_and_are_serialisable(
        self, adapter: ClientAdapter, code: ErrorCode, status: int
    ) -> None:
        response = adapter.render_error(ClientError(code, "msg", (FieldIssue("a.b[0]", "why"),)))

        assert response.status_code == status
        assert_json_serialisable(response.body)
        assert "a.b[0]" in str(response.body)
        assert "msg" in str(response.body)

    def test_each_adapter_renders_errors_in_its_own_format(self, adapter: ClientAdapter) -> None:
        """A cheap guard against copy-paste adapters: each one renders errors its own way."""
        mine = adapter.render_error(ClientError(ErrorCode.NOT_FOUND, "x")).body
        others = [
            other.render_error(ClientError(ErrorCode.NOT_FOUND, "x")).body
            for other in ADAPTERS
            if other.client_id != adapter.client_id
        ]
        assert all(mine != other for other in others)
