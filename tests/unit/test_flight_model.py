"""The Flight entity: which fields each status requires or forbids."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import FlightValidationError
from app.domain.models import (
    LIVE_FLIGHT_STATES,
    TERMINAL_FLIGHT_STATES,
    DenialCode,
    FlightStatus,
    PermissionDenial,
)
from tests.conftest import (
    DRONE_ID,
    T0,
    build_flight,
    build_mission,
    build_permission,
)

S = FlightStatus
NAIVE = datetime(2026, 11, 11, 9, 0)


@pytest.mark.parametrize("status", list(FlightStatus))
def test_a_consistent_flight_can_be_built_in_every_status(status: FlightStatus) -> None:
    assert build_flight(status).status is status


class TestStatusSets:
    def test_live_states(self) -> None:
        assert {
            S.PLANNED,
            S.PERMISSION_GRANTED,
            S.MISSION_UPLOADED,
            S.STARTED,
            S.PAUSED,
        } == LIVE_FLIGHT_STATES

    def test_terminal_states(self) -> None:
        assert {
            S.PERMISSION_DENIED,
            S.UPLOAD_FAILED,
            S.COMPLETED,
            S.ABORTED,
            S.CANCELLED,
        } == TERMINAL_FLIGHT_STATES

    def test_live_and_terminal_partition_all_statuses(self) -> None:
        assert set(FlightStatus) == LIVE_FLIGHT_STATES | TERMINAL_FLIGHT_STATES
        assert not LIVE_FLIGHT_STATES & TERMINAL_FLIGHT_STATES

    def test_properties(self) -> None:
        assert S.STARTED.is_live
        assert not S.STARTED.is_terminal
        assert S.COMPLETED.is_terminal
        assert not S.COMPLETED.is_live


class TestPermission:
    @pytest.mark.parametrize(
        "status",
        [
            S.PERMISSION_GRANTED,
            S.MISSION_UPLOADED,
            S.UPLOAD_FAILED,
            S.STARTED,
            S.PAUSED,
            S.COMPLETED,
            S.ABORTED,
        ],
    )
    def test_required_once_granted(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="requires a permission"):
            build_flight(status, permission=None)

    @pytest.mark.parametrize("status", [S.PLANNED, S.PERMISSION_DENIED])
    def test_forbidden_before_a_grant(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="cannot have a permission"):
            build_flight(status, permission=build_permission())

    @pytest.mark.parametrize("with_permission", [True, False])
    def test_optional_on_a_cancelled_flight(self, with_permission: bool) -> None:
        permission = build_permission() if with_permission else None
        assert build_flight(S.CANCELLED, permission=permission).permission == permission

    @pytest.mark.parametrize(
        "overrides",
        [{"flight_id": UUID(int=0x999)}, {"drone_id": UUID(int=0x999)}],
    )
    def test_must_be_for_this_flight_and_drone(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(FlightValidationError, match="different flight or drone"):
            build_flight(S.PERMISSION_GRANTED, permission=build_permission(**overrides))


class TestDenial:
    def test_only_a_denied_flight_carries_a_denial(self) -> None:
        denial = PermissionDenial(DenialCode.NO_FLY_ZONE, "zone")
        with pytest.raises(FlightValidationError, match="denial"):
            build_flight(S.PLANNED, denial=denial)

    def test_a_denied_flight_must_carry_one(self) -> None:
        with pytest.raises(FlightValidationError, match="denial"):
            build_flight(S.PERMISSION_DENIED, denial=None)


class TestMissionAck:
    @pytest.mark.parametrize(
        "status", [S.MISSION_UPLOADED, S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED]
    )
    def test_required_once_uploaded(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="mission ack"):
            build_flight(status, acknowledged_checksum=None)

    @pytest.mark.parametrize(
        "status", [S.PLANNED, S.PERMISSION_GRANTED, S.PERMISSION_DENIED, S.UPLOAD_FAILED]
    )
    def test_forbidden_before_an_upload(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="cannot have a mission ack"):
            build_flight(status, acknowledged_checksum=build_mission().checksum)

    def test_must_match_the_mission(self) -> None:
        with pytest.raises(FlightValidationError, match="does not match the mission"):
            build_flight(S.MISSION_UPLOADED, acknowledged_checksum="0" * 64)

    def test_a_cancelled_flight_may_keep_the_ack_of_an_upload_that_happened(self) -> None:
        flight = build_flight(
            S.CANCELLED,
            permission=build_permission(),
            acknowledged_checksum=build_mission().checksum,
        )
        assert flight.acknowledged_checksum is not None

    def test_an_ack_needs_a_permission(self) -> None:
        with pytest.raises(FlightValidationError, match="without a permission"):
            build_flight(
                S.CANCELLED, permission=None, acknowledged_checksum=build_mission().checksum
            )


class TestStartedAt:
    @pytest.mark.parametrize("status", [S.STARTED, S.PAUSED, S.COMPLETED, S.ABORTED])
    def test_required_once_started(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="started_at"):
            build_flight(status, started_at=None)

    @pytest.mark.parametrize(
        "status",
        [
            S.PLANNED,
            S.PERMISSION_GRANTED,
            S.PERMISSION_DENIED,
            S.MISSION_UPLOADED,
            S.UPLOAD_FAILED,
            S.CANCELLED,
        ],
    )
    def test_forbidden_before_a_start(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="started_at"):
            build_flight(status, started_at=T0)

    def test_must_be_timezone_aware(self) -> None:
        with pytest.raises(FlightValidationError, match="started_at must be timezone-aware"):
            build_flight(S.STARTED, started_at=NAIVE)


class TestReason:
    @pytest.mark.parametrize("status", [S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED])
    def test_required_when_a_flight_ends_early(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="reason"):
            build_flight(status, failure_reason=None)

    @pytest.mark.parametrize("status", [S.PLANNED, S.STARTED, S.PAUSED, S.COMPLETED])
    def test_forbidden_otherwise(self, status: FlightStatus) -> None:
        with pytest.raises(FlightValidationError, match="reason"):
            build_flight(status, failure_reason="why")

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_must_not_be_blank(self, reason: str) -> None:
        with pytest.raises(FlightValidationError, match="blank"):
            build_flight(S.ABORTED, failure_reason=reason)


class TestOther:
    @pytest.mark.parametrize("field", ["created_at", "updated_at"])
    def test_timestamps_must_be_timezone_aware(self, field: str) -> None:
        with pytest.raises(FlightValidationError, match=field):
            build_flight(**{field: NAIVE})

    def test_is_immutable(self) -> None:
        flight = build_flight()
        with pytest.raises(FrozenInstanceError):
            flight.status = S.STARTED  # type: ignore[misc]

    def test_status_cannot_be_assigned_via_setattr(self) -> None:
        flight = build_flight(S.MISSION_UPLOADED)
        with pytest.raises(FrozenInstanceError):
            setattr(flight, "status", S.STARTED)  # noqa: B010
        assert flight.status is S.MISSION_UPLOADED

    def test_a_flight_always_carries_its_mission(self) -> None:
        mission = build_mission()
        assert build_flight(mission=mission).mission is mission

    def test_the_default_drone_is_the_one_in_the_permission(self) -> None:
        flight = build_flight(S.PERMISSION_GRANTED)
        assert flight.permission is not None
        assert flight.permission.drone_id == flight.drone_id == DRONE_ID
