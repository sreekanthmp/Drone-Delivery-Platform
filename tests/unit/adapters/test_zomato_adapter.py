"""Zomato adapter (illustrative format)."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.base import ClientError, ErrorCode, FieldIssue
from app.adapters.zomato import ZomatoAdapter
from app.adapters.zomato.adapter import STATE_NAMES
from app.domain.models import Location, OrderStatus
from tests.payloads import drop_path, set_path, zomato_payload
from tests.unit.adapters.helpers import (
    MakeOrder,
    assert_json_serialisable,
    event_for,
    issue_paths,
)

adapter = ZomatoAdapter()


class TestParse:
    def test_coordinates_are_longitude_first_and_are_swapped_into_our_order(self) -> None:
        """Zomato sends [lon, lat]; Location takes (latitude, longitude)."""
        request = adapter.parse_order(zomato_payload())

        assert request.pickup == Location(latitude=12.9716, longitude=77.5946)
        assert request.dropoff == Location(latitude=12.9352, longitude=77.6245)

    def test_the_swapped_reading_would_be_a_different_place(self) -> None:
        # Guard for the test above: reading [lon, lat] as (lat, lon) gives different values.
        request = adapter.parse_order(zomato_payload())
        assert request.pickup != Location(latitude=77.5946, longitude=12.9716)

    def test_the_numeric_id_becomes_the_string_external_id(self) -> None:
        request = adapter.parse_order(zomato_payload())
        assert request.client_id == "zomato"
        assert request.external_order_id == "99812"

    def test_weight_is_converted_from_grams_to_kilograms(self) -> None:
        assert adapter.parse_order(zomato_payload()).package.weight_kg == pytest.approx(1.2)

    def test_tags_become_the_description(self) -> None:
        assert adapter.parse_order(zomato_payload()).package.description == "fragile, hot"

    def test_tags_are_optional(self) -> None:
        request = adapter.parse_order(drop_path(zomato_payload(), "order.tags"))
        assert request.package.description == ""

    def test_unknown_fields_are_ignored(self) -> None:
        payload = set_path(zomato_payload(), "order.restaurant_name", "x")
        payload = set_path(payload, "meta", {"source": "app"})
        assert adapter.parse_order(payload).external_order_id == "99812"

    def test_a_third_coordinate_element_is_rejected(self) -> None:
        payload = set_path(zomato_payload(), "order.pickup_point.coordinates", [77.5, 12.9, 900.0])
        assert any(
            p.startswith("order.pickup_point.coordinates") for p in issue_paths(adapter, payload)
        )

    @pytest.mark.parametrize(
        "path",
        [
            "order",
            "order.id",
            "order.pickup_point",
            "order.pickup_point.coordinates",
            "order.drop_point.coordinates",
            "order.package_weight_grams",
        ],
    )
    def test_each_required_field_is_reported_by_its_path(self, path: str) -> None:
        assert path in issue_paths(adapter, drop_path(zomato_payload(), path))

    @pytest.mark.parametrize(
        ("path", "value", "reported"),
        [
            # coordinates: [longitude, latitude]; element 0 is longitude, element 1 is latitude
            ("order.pickup_point.coordinates.0", 180.5, "order.pickup_point.coordinates[0]"),
            ("order.pickup_point.coordinates.1", 90.5, "order.pickup_point.coordinates[1]"),
            ("order.drop_point.coordinates.1", -91, "order.drop_point.coordinates[1]"),
            ("order.pickup_point.coordinates.0", "77.5", "order.pickup_point.coordinates[0]"),
            ("order.pickup_point.coordinates", [77.5], "order.pickup_point.coordinates[1]"),
            (
                "order.pickup_point.coordinates",
                {"lon": 1, "lat": 2},
                "order.pickup_point.coordinates",
            ),
            ("order.id", "99812", "order.id"),  # ids are numbers
            ("order.id", 0, "order.id"),
            ("order.id", -5, "order.id"),
            ("order.id", 1.5, "order.id"),
            ("order.id", True, "order.id"),
            ("order.package_weight_grams", 0, "order.package_weight_grams"),
            ("order.package_weight_grams", "1200", "order.package_weight_grams"),
            ("order.tags", "fragile", "order.tags"),
            ("order.tags.0", "", "order.tags[0]"),
            ("order.tags.0", 7, "order.tags[0]"),
        ],
    )
    def test_invalid_values_are_reported_at_the_right_path(
        self, path: str, value: Any, reported: str
    ) -> None:
        assert reported in issue_paths(adapter, set_path(zomato_payload(), path, value))

    def test_too_many_tags_are_rejected(self) -> None:
        payload = set_path(zomato_payload(), "order.tags", ["t"] * 21)
        assert "order.tags" in issue_paths(adapter, payload)

    def test_the_wrapper_object_is_required(self) -> None:
        assert issue_paths(adapter, zomato_payload()["order"]) == ["order"]


class TestRenderOrder:
    def test_a_new_order_is_201_and_the_id_is_a_number_again(self, make_order: MakeOrder) -> None:
        order = make_order(OrderStatus.CREATED, client_id="zomato", external_order_id="99812")

        response = adapter.render_order(order, created=True)

        assert response.status_code == 201
        assert response.body == {
            "success": True,
            "order": {
                "id": 99812,
                "platform_ref": str(order.id),
                "state": "accepted",
                "updated_at": "2026-11-11T09:00:00+00:00",
            },
        }

    def test_a_repeat_or_lookup_is_200(self, make_order: MakeOrder) -> None:
        order = make_order(client_id="zomato", external_order_id="7")
        assert adapter.render_order(order, created=False).status_code == 200


class TestStateVocabulary:
    def test_every_internal_status_has_a_zomato_state(self) -> None:
        assert set(STATE_NAMES) == set(OrderStatus)

    def test_the_mapping(self) -> None:
        assert {s.value: n for s, n in STATE_NAMES.items()} == {
            "created": "accepted",
            "assigned": "dispatch_assigned",
            "picked_up": "picked_up",
            "in_flight": "in_transit",
            "arriving": "arriving",
            "delivered": "delivered",
            "failed": "failed",
            "cancelled": "cancelled",
            "returned": "returned",
        }

    def test_no_two_statuses_collapse_into_one(self) -> None:
        assert len(set(STATE_NAMES.values())) == len(STATE_NAMES)

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_status_updates_use_the_zomato_vocabulary(
        self, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        order = make_order(status, client_id="zomato", external_order_id="99812")

        update = adapter.render_status_update(order, event_for(order, status))

        assert update == {
            "order_id": 99812,
            "state": STATE_NAMES[status],
            "event_ts": "2026-11-11T09:00:00+00:00",
            "event_ref": "00000000-0000-0000-0000-0000000000e1",
        }
        assert_json_serialisable(update)


class TestErrors:
    def test_invalid_request(self) -> None:
        response = adapter.render_error(
            ClientError(
                ErrorCode.INVALID_REQUEST,
                "bad",
                (FieldIssue("order.pickup_point.coordinates[1]", "too large"),),
            )
        )

        assert response.status_code == 422
        assert response.body == {
            "success": False,
            "error": {
                "type": "invalid_request",
                "message": "bad",
                "fields": [{"path": "order.pickup_point.coordinates[1]", "message": "too large"}],
            },
        }

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.CONFLICT, 409),
            (ErrorCode.UNAVAILABLE, 503),
            (ErrorCode.INTERNAL_ERROR, 500),
        ],
    )
    def test_other_errors(self, code: ErrorCode, status: int) -> None:
        response = adapter.render_error(ClientError(code, "x"))
        assert response.status_code == status
        assert response.body["success"] is False
        assert response.body["error"]["type"] == code.value
