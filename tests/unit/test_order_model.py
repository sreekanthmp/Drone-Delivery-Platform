"""Order, Location and Package value objects and invariants."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest
from app.domain.exceptions import OrderValidationError
from app.domain.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    Location,
    Order,
    OrderStatus,
    Package,
)

from tests.conftest import DRONE_ID, STATUSES_WITH_DRONE, T0

S = OrderStatus
MakeOrder = Callable[..., Order]


class TestLocation:
    @pytest.mark.parametrize(
        ("lat", "lon"),
        [(0, 0), (90, 180), (-90, -180), (12.9716, 77.5946), (-33.8688, 151.2093)],
    )
    def test_valid_coordinates(self, lat: float, lon: float) -> None:
        location = Location(latitude=lat, longitude=lon)
        assert (location.latitude, location.longitude) == (lat, lon)

    @pytest.mark.parametrize("lat", [90.0001, -90.0001, 1000, math.nan, math.inf, -math.inf])
    def test_invalid_latitude(self, lat: float) -> None:
        with pytest.raises(OrderValidationError, match="latitude"):
            Location(latitude=lat, longitude=0)

    @pytest.mark.parametrize("lon", [180.0001, -180.0001, 1000, math.nan, math.inf, -math.inf])
    def test_invalid_longitude(self, lon: float) -> None:
        with pytest.raises(OrderValidationError, match="longitude"):
            Location(latitude=0, longitude=lon)

    def test_locations_compare_by_value(self) -> None:
        assert Location(1.0, 2.0) == Location(1.0, 2.0)
        assert Location(1.0, 2.0) != Location(1.0, 2.5)

    def test_is_immutable(self) -> None:
        location = Location(1.0, 2.0)
        with pytest.raises(FrozenInstanceError):
            location.latitude = 5.0  # type: ignore[misc]


class TestPackage:
    @pytest.mark.parametrize("weight", [0.001, 1, 2.5, 25])
    def test_valid_weight(self, weight: float) -> None:
        assert Package(weight_kg=weight).weight_kg == weight

    @pytest.mark.parametrize("weight", [0, -1, -0.5, math.nan, math.inf])
    def test_invalid_weight(self, weight: float) -> None:
        with pytest.raises(OrderValidationError, match="weight"):
            Package(weight_kg=weight)

    def test_description_is_optional(self) -> None:
        assert Package(weight_kg=1).description == ""

    def test_is_immutable(self) -> None:
        package = Package(weight_kg=1)
        with pytest.raises(FrozenInstanceError):
            package.weight_kg = 2  # type: ignore[misc]


class TestOrderStatus:
    def test_has_exactly_the_nine_spec_statuses(self) -> None:
        assert {s.value for s in OrderStatus} == {
            "created",
            "assigned",
            "picked_up",
            "in_flight",
            "arriving",
            "delivered",
            "failed",
            "cancelled",
            "returned",
        }

    def test_active_and_terminal_flags(self) -> None:
        assert {s for s in OrderStatus if s.is_active} == ACTIVE_STATES
        assert {s for s in OrderStatus if s.is_terminal} == TERMINAL_STATES

    def test_is_a_string_for_serialisation(self) -> None:
        assert S.IN_FLIGHT == "in_flight"
        assert S("picked_up") is S.PICKED_UP


class TestOrderInvariants:
    def test_valid_order(self, make_order: MakeOrder) -> None:
        order = make_order(S.CREATED)
        assert order.status is S.CREATED
        assert order.drone_id is None

    @pytest.mark.parametrize("field", ["client_id", "external_order_id"])
    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_blank_identifiers_are_rejected(
        self, make_order: MakeOrder, field: str, value: str
    ) -> None:
        with pytest.raises(OrderValidationError, match=field):
            make_order(S.CREATED, **{field: value})

    def test_pickup_and_dropoff_must_differ(self, make_order: MakeOrder, pickup: Location) -> None:
        with pytest.raises(OrderValidationError, match="differ"):
            make_order(S.CREATED, dropoff=pickup)

    @pytest.mark.parametrize("field", ["created_at", "updated_at"])
    def test_timestamps_must_be_timezone_aware(self, make_order: MakeOrder, field: str) -> None:
        with pytest.raises(OrderValidationError, match=field):
            make_order(S.CREATED, **{field: datetime(2026, 11, 11, 9, 0)})

    def test_non_utc_aware_timestamps_are_accepted(self, make_order: MakeOrder) -> None:
        from datetime import timedelta, timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        order = make_order(S.CREATED, created_at=datetime(2026, 11, 11, 14, 30, tzinfo=ist))
        assert order.created_at.utcoffset() == timedelta(hours=5, minutes=30)

    def test_created_order_cannot_have_a_drone(self, make_order: MakeOrder) -> None:
        with pytest.raises(OrderValidationError, match="created"):
            make_order(S.CREATED, drone_id=DRONE_ID)

    @pytest.mark.parametrize("status", sorted(STATUSES_WITH_DRONE))
    def test_statuses_past_created_require_a_drone(
        self, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        with pytest.raises(OrderValidationError, match="drone_id"):
            make_order(status, drone_id=None)

    @pytest.mark.parametrize("status", [S.FAILED, S.CANCELLED])
    @pytest.mark.parametrize("drone_id", [None, DRONE_ID])
    def test_failed_and_cancelled_orders_may_or_may_not_have_a_drone(
        self, make_order: MakeOrder, status: OrderStatus, drone_id: UUID | None
    ) -> None:
        assert make_order(status, drone_id=drone_id).drone_id == drone_id


class TestOrderImmutability:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("status", S.DELIVERED),
            ("drone_id", DRONE_ID),
            ("client_id", "other"),
            ("updated_at", datetime(2030, 1, 1, tzinfo=UTC)),
        ],
    )
    def test_fields_cannot_be_assigned(
        self, make_order: MakeOrder, field: str, value: object
    ) -> None:
        order = make_order(S.CREATED)
        with pytest.raises(FrozenInstanceError):
            setattr(order, field, value)

    def test_orders_have_no_instance_dict(self, make_order: MakeOrder) -> None:
        # slots=True: no ad-hoc attributes can be attached either.
        with pytest.raises((AttributeError, TypeError)):
            make_order(S.CREATED).extra = 1  # type: ignore[attr-defined]


class TestRehydrate:
    def test_rebuilds_an_order_in_any_status(self, make_order: MakeOrder) -> None:
        for status in OrderStatus:
            assert make_order(status).status is status

    def test_preserves_every_field(self, make_order: MakeOrder) -> None:
        order = make_order(S.ASSIGNED, id=UUID(int=42), external_order_id="EXT-9")
        assert order.id == UUID(int=42)
        assert order.external_order_id == "EXT-9"
        assert order.created_at == order.updated_at == T0
        assert order.drone_id == DRONE_ID

    def test_still_enforces_invariants(self, make_order: MakeOrder) -> None:
        with pytest.raises(OrderValidationError):
            make_order(S.DELIVERED, drone_id=None)
