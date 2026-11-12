"""Swiggy adapter (illustrative format)."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.base import ClientError, ErrorCode, FieldIssue
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.swiggy.adapter import STATUS_NAMES
from app.domain.models import Location, OrderStatus
from tests.payloads import drop_path, set_path, swiggy_payload
from tests.unit.adapters.helpers import (
    MakeOrder,
    assert_json_serialisable,
    event_for,
    issue_paths,
)

adapter = SwiggyAdapter()


class TestParse:
    def test_maps_restaurant_to_pickup_and_customer_to_dropoff(self) -> None:
        request = adapter.parse_order(swiggy_payload())

        assert request.client_id == "swiggy"
        assert request.external_order_id == "SW-100234"
        assert request.pickup == Location(latitude=12.9716, longitude=77.5946)
        assert request.dropoff == Location(latitude=12.9352, longitude=77.6245)

    def test_package_weight_is_the_sum_of_item_weights_in_kilograms(self) -> None:
        # 2 x 450 g + 1 x 200 g = 1100 g
        assert adapter.parse_order(swiggy_payload()).package.weight_kg == pytest.approx(1.1)

    def test_a_single_item(self) -> None:
        payload = set_path(
            swiggy_payload(), "items", [{"name": "Dosa", "quantity": 3, "weight_grams": 100}]
        )
        request = adapter.parse_order(payload)
        assert request.package.weight_kg == pytest.approx(0.3)
        assert request.package.description == "3 x Dosa"

    def test_description_lists_the_items(self) -> None:
        assert adapter.parse_order(swiggy_payload()).package.description == "2 x Biryani, 1 x Raita"

    def test_a_very_long_item_list_is_truncated_not_rejected(self) -> None:
        items = [
            {"name": f"Item number {i:03d} with a long name", "quantity": 1, "weight_grams": 1}
            for i in range(100)
        ]
        request = adapter.parse_order(set_path(swiggy_payload(), "items", items))

        assert len(request.package.description) == 500
        assert request.package.description.endswith("…")

    def test_unknown_fields_are_ignored(self) -> None:
        payload = set_path(swiggy_payload(), "special_instructions", "no onions")
        payload = set_path(payload, "restaurant.name", "Meghana Foods")
        assert adapter.parse_order(payload).external_order_id == "SW-100234"

    @pytest.mark.parametrize(
        "path",
        [
            "swiggy_order_id",
            "restaurant",
            "restaurant.location",
            "restaurant.location.latitude",
            "customer",
            "customer.address",
            "customer.address.longitude",
            "items",
            "items.0.name",
            "items.0.quantity",
            "items.1.weight_grams",
        ],
    )
    def test_each_required_field_is_reported_by_its_path(self, path: str) -> None:
        expected = path.replace(".0", "[0]").replace(".1", "[1]")
        assert expected in issue_paths(adapter, drop_path(swiggy_payload(), path))

    @pytest.mark.parametrize(
        ("path", "value", "reported"),
        [
            ("restaurant.location.latitude", 91, "restaurant.location.latitude"),
            ("customer.address.longitude", -181, "customer.address.longitude"),
            ("items", [], "items"),
            ("items.0.quantity", 0, "items[0].quantity"),
            ("items.0.quantity", 1.5, "items[0].quantity"),
            ("items.0.quantity", "2", "items[0].quantity"),
            ("items.1.weight_grams", 0, "items[1].weight_grams"),
            ("items.1.weight_grams", -5, "items[1].weight_grams"),
            ("items.1.weight_grams", "200", "items[1].weight_grams"),
            ("items.0.name", "", "items[0].name"),
            ("swiggy_order_id", "  ", "swiggy_order_id"),
            ("swiggy_order_id", 12345, "swiggy_order_id"),
            ("items", "not a list", "items"),
        ],
    )
    def test_invalid_values_are_reported_at_the_right_path(
        self, path: str, value: Any, reported: str
    ) -> None:
        assert reported in issue_paths(adapter, set_path(swiggy_payload(), path, value))

    def test_too_many_items_are_rejected(self) -> None:
        items = [{"name": "x", "quantity": 1, "weight_grams": 1}] * 101
        assert "items" in issue_paths(adapter, set_path(swiggy_payload(), "items", items))

    def test_a_zomato_shaped_payload_is_not_a_swiggy_order(self) -> None:
        from tests.payloads import zomato_payload

        assert issue_paths(adapter, zomato_payload())


class TestRenderOrder:
    def test_a_new_order_is_201_with_the_swiggy_format(self, make_order: MakeOrder) -> None:
        order = make_order(OrderStatus.CREATED, client_id="swiggy", external_order_id="SW-100234")

        response = adapter.render_order(order, created=True)

        assert response.status_code == 201
        assert response.body == {
            "status": "success",
            "data": {
                "swiggy_order_id": "SW-100234",
                "drone_order_ref": str(order.id),
                "order_status": "PLACED",
                "last_updated": "2026-11-11T09:00:00+00:00",
            },
        }

    def test_a_repeat_or_lookup_is_200(self, make_order: MakeOrder) -> None:
        assert adapter.render_order(make_order(), created=False).status_code == 200


class TestStatusVocabulary:
    def test_every_internal_status_has_a_swiggy_status(self) -> None:
        assert set(STATUS_NAMES) == set(OrderStatus)

    def test_the_mapping(self) -> None:
        assert {s.value: n for s, n in STATUS_NAMES.items()} == {
            "created": "PLACED",
            "assigned": "DRONE_ASSIGNED",
            "picked_up": "PICKED_UP",
            "in_flight": "OUT_FOR_DELIVERY",
            "arriving": "ARRIVING",
            "delivered": "DELIVERED",
            "failed": "DELIVERY_FAILED",
            "cancelled": "CANCELLED",
            "returned": "RETURNED_TO_RESTAURANT",
        }

    def test_no_two_statuses_collapse_into_one(self) -> None:
        assert len(set(STATUS_NAMES.values())) == len(STATUS_NAMES)

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_status_updates_use_the_swiggy_vocabulary(
        self, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        order = make_order(status, client_id="swiggy", external_order_id="SW-1")

        update = adapter.render_status_update(order, event_for(order, status))

        assert update == {
            "swiggy_order_id": "SW-1",
            "event": STATUS_NAMES[status],
            "timestamp": "2026-11-11T09:00:00+00:00",
            "reference": "00000000-0000-0000-0000-0000000000e1",
        }
        assert_json_serialisable(update)


class TestErrors:
    def test_invalid_request(self) -> None:
        response = adapter.render_error(
            ClientError(
                ErrorCode.INVALID_REQUEST, "bad", (FieldIssue("items[0].quantity", "too small"),)
            )
        )

        assert response.status_code == 422
        assert response.body == {
            "status": "error",
            "error_code": "INVALID_REQUEST",
            "message": "bad",
            "errors": [{"field": "items[0].quantity", "message": "too small"}],
        }

    @pytest.mark.parametrize(
        ("code", "status", "label"),
        [
            (ErrorCode.NOT_FOUND, 404, "NOT_FOUND"),
            (ErrorCode.INTERNAL_ERROR, 500, "INTERNAL_ERROR"),
        ],
    )
    def test_other_errors(self, code: ErrorCode, status: int, label: str) -> None:
        response = adapter.render_error(ClientError(code, "x"))
        assert (response.status_code, response.body["error_code"]) == (status, label)
        assert response.body["errors"] == []
