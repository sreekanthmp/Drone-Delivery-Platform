"""Live tracking messages, rendered in each partner's own format (Rule 3).

A tracking message carries the order's status in the partner's vocabulary and, while a drone is
carrying the order, where it is. It can carry nothing else: no drone, flight or battery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.adapters.base import ClientAdapter
from app.adapters.mock_client import MockClientAdapter
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.zomato import ZomatoAdapter
from app.domain.exceptions import TrackingValidationError
from app.domain.models import Location, OrderStatus, TrackingPosition
from tests.conftest import DRONE_ID, T0
from tests.unit.adapters.helpers import MakeOrder, assert_json_serialisable
from tests.unit.adapters.test_adapter_contract import ADAPTERS, IDS

S = OrderStatus
SEEN = datetime(2026, 11, 11, 9, 5, 30, tzinfo=UTC)
POSITION = TrackingPosition(Location(latitude=12.95, longitude=77.61), 55.5, SEEN)

# What each partner calls "in flight", and where it puts the position.
IN_FLIGHT_NAME = {"mock": "in_flight", "swiggy": "OUT_FOR_DELIVERY", "zomato": "in_transit"}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
class TestEveryAdapter:
    def test_it_is_json_serialisable_with_and_without_a_position(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        order = make_order(S.IN_FLIGHT)

        for position in (POSITION, None):
            assert_json_serialisable(adapter.render_tracking_update(order, position))

    def test_it_carries_the_partners_own_order_id_and_our_tracking_reference(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        order = make_order(S.IN_FLIGHT, external_order_id="12345")

        text = json.dumps(adapter.render_tracking_update(order, POSITION))

        assert "12345" in text
        assert str(order.id) in text

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_the_status_is_in_the_partners_vocabulary_like_everywhere_else(
        self, adapter: ClientAdapter, make_order: MakeOrder, status: OrderStatus
    ) -> None:
        order = make_order(status)
        from_order = json.dumps(adapter.render_order(order, created=False).body)

        tracking = json.dumps(adapter.render_tracking_update(order, None))

        # the partner's word for this status appears in both, and they agree
        name = {
            "mock": status.value,
            "swiggy": _swiggy_name(status),
            "zomato": _zomato_name(status),
        }[adapter.client_id]
        assert f'"{name}"' in from_order
        assert f'"{name}"' in tracking

    def test_without_a_position_the_position_is_null_not_missing(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        body = json.dumps(adapter.render_tracking_update(make_order(S.ASSIGNED), None))

        assert "null" in body

    def test_it_never_reveals_the_drone_the_flight_or_the_battery(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        order = make_order(S.IN_FLIGHT, drone_id=DRONE_ID)

        text = json.dumps(adapter.render_tracking_update(order, POSITION)).lower()

        assert str(DRONE_ID) not in text
        for word in ("battery", "flight_id", "mission", "permission", "drone_id", "phase"):
            assert word not in text, word

    def test_it_is_deterministic(self, adapter: ClientAdapter, make_order: MakeOrder) -> None:
        order = make_order(S.IN_FLIGHT)

        assert adapter.render_tracking_update(order, POSITION) == adapter.render_tracking_update(
            order, POSITION
        )

    def test_each_partner_renders_it_its_own_way(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        order = make_order(S.IN_FLIGHT)
        mine = adapter.render_tracking_update(order, POSITION)

        others = [
            other.render_tracking_update(order, POSITION)
            for other in ADAPTERS
            if other.client_id != adapter.client_id
        ]

        assert all(mine != other for other in others)

    def test_the_position_values_survive_exactly(
        self, adapter: ClientAdapter, make_order: MakeOrder
    ) -> None:
        text = json.dumps(adapter.render_tracking_update(make_order(S.IN_FLIGHT), POSITION))

        for value in ("12.95", "77.61", "55.5"):
            assert value in text


def _swiggy_name(status: OrderStatus) -> str:
    from app.adapters.swiggy.adapter import STATUS_NAMES

    return STATUS_NAMES[status]


def _zomato_name(status: OrderStatus) -> str:
    from app.adapters.zomato.adapter import STATE_NAMES

    return STATE_NAMES[status]


class TestMockShape:
    def test_exact_body(self, make_order: MakeOrder) -> None:
        order = make_order(S.IN_FLIGHT)

        body = MockClientAdapter().render_tracking_update(order, POSITION)

        assert body == {
            "order_id": "ORD-1",
            "tracking_id": str(order.id),
            "status": "in_flight",
            "updated_at": "2026-11-11T09:00:00+00:00",
            "position": {
                "lat": 12.95,
                "lng": 77.61,
                "alt_m": 55.5,
                "as_of": "2026-11-11T09:05:30+00:00",
            },
        }

    def test_no_position(self, make_order: MakeOrder) -> None:
        assert (
            MockClientAdapter().render_tracking_update(make_order(S.CREATED), None)["position"]
            is None
        )


class TestSwiggyShape:
    def test_exact_body(self, make_order: MakeOrder) -> None:
        order = make_order(S.IN_FLIGHT, external_order_id="SW-100234")

        body = SwiggyAdapter().render_tracking_update(order, POSITION)

        assert body == {
            "status": "success",
            "data": {
                "swiggy_order_id": "SW-100234",
                "drone_order_ref": str(order.id),
                "order_status": "OUT_FOR_DELIVERY",
                "last_updated": "2026-11-11T09:00:00+00:00",
                "live_location": {
                    "latitude": 12.95,
                    "longitude": 77.61,
                    "altitude_meters": 55.5,
                    "as_of": "2026-11-11T09:05:30+00:00",
                },
            },
        }


class TestZomatoShape:
    def test_exact_body_with_geojson_order_longitude_first(self, make_order: MakeOrder) -> None:
        order = make_order(S.IN_FLIGHT, external_order_id="99812")

        body = ZomatoAdapter().render_tracking_update(order, POSITION)

        assert body == {
            "success": True,
            "order": {
                "id": 99812,
                "platform_ref": str(order.id),
                "state": "in_transit",
                "updated_at": "2026-11-11T09:00:00+00:00",
                "location": {
                    "type": "Point",
                    "coordinates": [77.61, 12.95, 55.5],  # [longitude, latitude, altitude]
                    "as_of": "2026-11-11T09:05:30+00:00",
                },
            },
        }


class TestTrackingPosition:
    def test_it_holds_a_location_an_altitude_and_a_time(self) -> None:
        assert POSITION.location == Location(latitude=12.95, longitude=77.61)
        assert POSITION.altitude_m == 55.5

    @pytest.mark.parametrize("altitude", [-1.0, float("nan"), float("inf")])
    def test_an_impossible_altitude_is_refused(self, altitude: float) -> None:
        with pytest.raises(TrackingValidationError, match="altitude"):
            TrackingPosition(Location(latitude=1.0, longitude=1.0), altitude, SEEN)

    def test_a_naive_time_is_refused(self) -> None:
        with pytest.raises(TrackingValidationError, match="timezone"):
            TrackingPosition(Location(latitude=1.0, longitude=1.0), 1.0, T0.replace(tzinfo=None))

    def test_it_is_immutable_and_has_no_room_for_internal_details(self) -> None:
        assert set(TrackingPosition.__dataclass_fields__) == {
            "location",
            "altitude_m",
            "observed_at",
        }
