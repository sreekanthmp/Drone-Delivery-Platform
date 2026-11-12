"""Mock client adapter."""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.adapters.base import AdapterValidationError, ClientError, ErrorCode, FieldIssue
from app.adapters.mock_client import MockClientAdapter
from app.adapters.mock_client.adapter import STATUS_NAMES
from app.domain.models import Location, OrderStatus, Package
from tests.payloads import drop_path, mock_payload, set_path
from tests.unit.adapters.helpers import (
    MakeOrder,
    assert_json_serialisable,
    event_for,
    issue_paths,
)

adapter = MockClientAdapter()


class TestParse:
    def test_maps_the_payload_to_an_internal_order_request(self) -> None:
        request = adapter.parse_order(mock_payload())

        assert request.client_id == "mock"
        assert request.external_order_id == "ORD-1"
        assert request.pickup == Location(latitude=12.9716, longitude=77.5946)
        assert request.dropoff == Location(latitude=12.9352, longitude=77.6245)
        assert request.package == Package(weight_kg=1.5, description="2 meals")

    def test_description_is_optional(self) -> None:
        request = adapter.parse_order(drop_path(mock_payload(), "package.description"))
        assert request.package.description == ""

    def test_unknown_fields_are_ignored(self) -> None:
        payload = set_path(mock_payload(), "surprise", {"anything": 1})
        payload = set_path(payload, "pickup.altitude", 900)
        assert adapter.parse_order(payload).external_order_id == "ORD-1"

    def test_ids_are_stripped_of_surrounding_whitespace(self) -> None:
        request = adapter.parse_order(set_path(mock_payload(), "order_id", "  ORD-9 \n"))
        assert request.external_order_id == "ORD-9"

    def test_integer_coordinates_are_accepted(self) -> None:
        payload = set_path(set_path(mock_payload(), "pickup.lat", 13), "pickup.lng", 78)
        assert adapter.parse_order(payload).pickup == Location(latitude=13.0, longitude=78.0)

    @pytest.mark.parametrize(
        ("path", "value"),
        [("pickup.lat", 90), ("pickup.lat", -90), ("dropoff.lng", 180), ("dropoff.lng", -180)],
    )
    def test_boundary_coordinates_are_accepted(self, path: str, value: float) -> None:
        assert adapter.parse_order(set_path(mock_payload(), path, value))

    @pytest.mark.parametrize(
        "path",
        [
            "order_id",
            "pickup",
            "dropoff",
            "package",
            "pickup.lat",
            "pickup.lng",
            "dropoff.lat",
            "dropoff.lng",
            "package.weight_kg",
        ],
    )
    def test_each_required_field_is_reported_by_its_path(self, path: str) -> None:
        assert path in issue_paths(adapter, drop_path(mock_payload(), path))

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("pickup.lat", 90.0001),
            ("pickup.lat", -90.0001),
            ("pickup.lng", 180.5),
            ("dropoff.lng", -181),
            ("pickup.lat", math.nan),
            ("pickup.lat", math.inf),
            ("pickup.lat", "12.5"),  # numeric strings are not coerced
            ("pickup.lat", True),  # booleans are not numbers
            ("pickup.lat", None),
            ("pickup", [12.9, 77.5]),
            ("package.weight_kg", 0),
            ("package.weight_kg", -1),
            ("package.weight_kg", math.nan),
            ("package.weight_kg", "1.5"),
            ("package.description", 5),
            ("package.description", "x" * 501),
            ("order_id", ""),
            ("order_id", "   "),
            ("order_id", 123),
            ("order_id", "x" * 129),
            ("order_id", None),
        ],
    )
    def test_invalid_values_are_rejected_at_their_path(self, path: str, value: Any) -> None:
        assert path in issue_paths(adapter, set_path(mock_payload(), path, value))

    def test_an_empty_payload_reports_every_missing_field(self) -> None:
        assert set(issue_paths(adapter, {})) == {"order_id", "pickup", "dropoff", "package"}

    def test_the_error_never_echoes_the_offending_value(self) -> None:
        payload = set_path(mock_payload(), "pickup.lat", "SECRET-VALUE-123")
        with pytest.raises(AdapterValidationError) as exc_info:
            adapter.parse_order(payload)

        assert "SECRET-VALUE-123" not in str(exc_info.value)
        assert all("SECRET-VALUE-123" not in i.message for i in exc_info.value.issues)

    def test_the_client_id_is_stable(self) -> None:
        assert adapter.client_id == "mock"


class TestRenderOrder:
    def test_a_new_order_is_201_with_the_mock_format(self, make_order: MakeOrder) -> None:
        order = make_order(OrderStatus.CREATED, client_id="mock", external_order_id="ORD-1")

        response = adapter.render_order(order, created=True)

        assert response.status_code == 201
        assert response.body == {
            "order_id": "ORD-1",
            "tracking_id": str(order.id),
            "status": "created",
            "created_at": "2026-11-11T09:00:00+00:00",
            "updated_at": "2026-11-11T09:00:00+00:00",
        }

    def test_a_repeat_or_lookup_is_200(self, make_order: MakeOrder) -> None:
        assert adapter.render_order(make_order(), created=False).status_code == 200

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_every_status_renders(self, make_order: MakeOrder, status: OrderStatus) -> None:
        response = adapter.render_order(make_order(status), created=False)
        assert response.body["status"] == STATUS_NAMES[status]
        assert_json_serialisable(response.body)


class TestStatusUpdates:
    def test_the_status_vocabulary_is_the_platforms_own(self) -> None:
        assert dict(STATUS_NAMES) == {s: s.value for s in OrderStatus}

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_every_status_is_translated(self, make_order: MakeOrder, status: OrderStatus) -> None:
        order = make_order(status)

        update = adapter.render_status_update(order, event_for(order, status))

        assert update["order_id"] == "ORD-1"
        assert update["status"] == status.value
        assert update["event_id"] == "00000000-0000-0000-0000-0000000000e1"
        assert update["occurred_at"] == "2026-11-11T09:00:00+00:00"
        assert_json_serialisable(update)


class TestErrors:
    def test_invalid_request_is_422_with_field_details(self) -> None:
        response = adapter.render_error(
            ClientError(
                ErrorCode.INVALID_REQUEST,
                "bad payload",
                (FieldIssue("pickup.lat", "Input should be less than or equal to 90"),),
            )
        )

        assert response.status_code == 422
        assert response.body == {
            "error": {
                "code": "invalid_request",
                "message": "bad payload",
                "details": [
                    {"field": "pickup.lat", "message": "Input should be less than or equal to 90"}
                ],
            }
        }

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (ErrorCode.INVALID_REQUEST, 422),
            (ErrorCode.NOT_FOUND, 404),
            (ErrorCode.INTERNAL_ERROR, 500),
        ],
    )
    def test_status_codes(self, code: ErrorCode, status: int) -> None:
        response = adapter.render_error(ClientError(code, "x"))
        assert response.status_code == status
        assert response.body["error"]["code"] == code.value
        assert_json_serialisable(response.body)
