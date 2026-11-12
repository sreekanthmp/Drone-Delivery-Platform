"""Permission models: grants, denials, validations and requests."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from app.domain.exceptions import PermissionValidationError
from app.domain.models import (
    DenialCode,
    PermissionDenial,
    PermissionRequest,
    PermissionStatus,
    PermissionValidation,
)
from tests.conftest import (
    DRONE_ID,
    FLIGHT_ID,
    HOME,
    ORDER_ID,
    PICKUP,
    T0,
    build_mission,
    build_permission,
)

NAIVE = datetime(2026, 11, 11, 9, 0)


class TestFlightPermission:
    def test_a_valid_permission(self) -> None:
        permission = build_permission()
        assert permission.valid_until - permission.valid_from == timedelta(minutes=15)

    @pytest.mark.parametrize("field", ["permission_id", "issuer"])
    @pytest.mark.parametrize("value", ["", "  "])
    def test_blank_identifiers_are_rejected(self, field: str, value: str) -> None:
        with pytest.raises(PermissionValidationError, match=field):
            build_permission(**{field: value})

    @pytest.mark.parametrize("field", ["issued_at", "valid_from", "valid_until"])
    def test_timestamps_must_be_timezone_aware(self, field: str) -> None:
        with pytest.raises(PermissionValidationError, match=field):
            build_permission(**{field: NAIVE})

    @pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
    def test_it_must_end_after_it_begins(self, delta: timedelta) -> None:
        with pytest.raises(PermissionValidationError, match="after valid_from"):
            build_permission(valid_until=T0 + delta)

    def test_is_immutable(self) -> None:
        with pytest.raises(FrozenInstanceError):
            build_permission().valid_until = T0  # type: ignore[misc]


class TestPermissionDenial:
    def test_a_denial_has_a_code_and_a_reason(self) -> None:
        denial = PermissionDenial(DenialCode.NO_FLY_ZONE, "military base")
        assert (denial.code, denial.reason) == (DenialCode.NO_FLY_ZONE, "military base")

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_a_reason_is_required(self, reason: str) -> None:
        with pytest.raises(PermissionValidationError, match="reason"):
            PermissionDenial(DenialCode.REJECTED, reason)

    def test_the_reason_is_capped(self) -> None:
        PermissionDenial(DenialCode.REJECTED, "x" * 500)
        with pytest.raises(PermissionValidationError, match="longer"):
            PermissionDenial(DenialCode.REJECTED, "x" * 501)

    def test_the_codes(self) -> None:
        assert {c.value for c in DenialCode} == {
            "no_fly_zone",
            "airspace_closed",
            "provider_unavailable",
            "rejected",
        }


class TestPermissionValidation:
    def test_a_valid_answer_reports_when_it_ends(self) -> None:
        validation = PermissionValidation("P", PermissionStatus.VALID, T0, T0 + timedelta(hours=1))
        assert validation.status is PermissionStatus.VALID

    def test_a_valid_answer_without_an_end_is_rejected(self) -> None:
        with pytest.raises(PermissionValidationError, match="when it ends"):
            PermissionValidation("P", PermissionStatus.VALID, T0)

    @pytest.mark.parametrize(
        "status", [PermissionStatus.EXPIRED, PermissionStatus.REVOKED, PermissionStatus.UNKNOWN]
    )
    def test_other_answers_need_no_end(self, status: PermissionStatus) -> None:
        assert PermissionValidation("P", status, T0).valid_until is None

    def test_timestamps_must_be_timezone_aware(self) -> None:
        with pytest.raises(PermissionValidationError, match="checked_at"):
            PermissionValidation("P", PermissionStatus.UNKNOWN, NAIVE)
        with pytest.raises(PermissionValidationError, match="valid_until"):
            PermissionValidation("P", PermissionStatus.VALID, T0, NAIVE)


class TestPermissionRequest:
    def test_built_from_a_mission(self) -> None:
        mission = build_mission()

        request = PermissionRequest.from_mission(
            flight_id=FLIGHT_ID,
            order_id=ORDER_ID,
            drone_id=DRONE_ID,
            mission=mission,
            requested_start=T0,
        )

        assert request.route == mission.route
        assert request.estimated_duration_s == mission.estimated_duration_s
        assert (request.flight_id, request.order_id, request.drone_id) == (
            FLIGHT_ID,
            ORDER_ID,
            DRONE_ID,
        )

    def test_a_route_needs_two_points(self) -> None:
        with pytest.raises(PermissionValidationError, match="2\\+ points"):
            PermissionRequest(FLIGHT_ID, ORDER_ID, DRONE_ID, (HOME,), T0, 60)

    def test_a_route_given_as_a_list_becomes_a_tuple(self) -> None:
        request = PermissionRequest(
            FLIGHT_ID,
            ORDER_ID,
            DRONE_ID,
            [HOME, PICKUP],  # type: ignore[arg-type]
            T0,
            60,
        )
        assert isinstance(request.route, tuple)

    def test_the_start_must_be_timezone_aware(self) -> None:
        with pytest.raises(PermissionValidationError, match="requested_start"):
            PermissionRequest(FLIGHT_ID, ORDER_ID, DRONE_ID, (HOME, PICKUP), NAIVE, 60)

    @pytest.mark.parametrize("duration", [0, -5, math.nan, math.inf])
    def test_the_duration_must_be_positive(self, duration: float) -> None:
        with pytest.raises(PermissionValidationError, match="estimated_duration_s"):
            PermissionRequest(FLIGHT_ID, ORDER_ID, DRONE_ID, (HOME, PICKUP), T0, duration)

    def test_ids_are_uuids(self) -> None:
        request = PermissionRequest(FLIGHT_ID, ORDER_ID, DRONE_ID, (HOME, PICKUP), T0, 60)
        assert all(isinstance(i, UUID) for i in (request.flight_id, request.order_id))
