"""The drone link port and its in-memory implementation (Rule 1 and the Rule 2 token)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.dispatch.in_memory_link import InMemoryDroneLink
from app.dispatch.link import (
    CommandRejectedError,
    DroneLink,
    DroneLinkError,
    MissionAck,
    NoAcknowledgementError,
)
from app.domain.flight_state_machine import FlightStateMachine, StartAuthorization
from app.domain.models import EventSource, FlightStatus, PermissionStatus, PermissionValidation
from tests.conftest import DRONE_ID, FakeClock, build_flight, build_mission

OTHER_DRONE = UUID(int=0xD1)


@pytest.fixture
def link(clock: FakeClock) -> InMemoryDroneLink:
    return InMemoryDroneLink(clock=clock)


def authorization(clock: FakeClock, **overrides: Any) -> StartAuthorization:
    """A real authorisation, obtained the only way possible: from the flight state machine."""
    flight = build_flight(FlightStatus.MISSION_UPLOADED)
    assert flight.permission is not None
    result = FlightStateMachine(clock=clock).transition(
        flight,
        FlightStatus.STARTED,
        source=EventSource.DISPATCH,
        validation=PermissionValidation(
            flight.permission.permission_id,
            PermissionStatus.VALID,
            clock(),
            flight.permission.valid_until,
        ),
    )
    assert result.start_authorization is not None
    if overrides:
        return StartAuthorization(**{**vars_of(result.start_authorization), **overrides})
    return result.start_authorization


def vars_of(a: StartAuthorization) -> dict[str, Any]:
    return {
        "flight_id": a.flight_id,
        "drone_id": a.drone_id,
        "permission_id": a.permission_id,
        "mission_checksum": a.mission_checksum,
        "authorized_at": a.authorized_at,
        "valid_until": a.valid_until,
    }


class TestThePort:
    def test_it_offers_exactly_five_high_level_operations(self) -> None:
        """Rule 1: upload a mission, start it, pause it, return home, land. Nothing else."""
        operations = {name for name in vars(DroneLink) if not name.startswith("_")}

        assert operations == {
            "upload_mission",
            "start_mission",
            "pause_mission",
            "return_to_home",
            "land",
        }

    def test_the_two_failure_kinds_are_distinct_subclasses_of_one_base(self) -> None:
        assert issubclass(CommandRejectedError, DroneLinkError)
        assert issubclass(NoAcknowledgementError, DroneLinkError)
        assert not issubclass(CommandRejectedError, NoAcknowledgementError)
        assert not issubclass(NoAcknowledgementError, CommandRejectedError)


class TestUpload:
    async def test_the_drone_acknowledges_with_the_checksum_of_what_it_received(
        self, link: InMemoryDroneLink
    ) -> None:
        mission = build_mission()

        ack = await link.upload_mission(DRONE_ID, mission)

        assert ack == MissionAck(DRONE_ID, mission.mission_id, mission.checksum)
        assert link.on_board[DRONE_ID] == mission

    async def test_a_new_upload_replaces_the_one_on_board(self, link: InMemoryDroneLink) -> None:
        await link.upload_mission(DRONE_ID, build_mission())
        newer = build_mission(mission_id=UUID(int=0x52))

        await link.upload_mission(DRONE_ID, newer)

        assert link.on_board[DRONE_ID] == newer

    async def test_a_corrupt_acknowledgement_happens_once(self, link: InMemoryDroneLink) -> None:
        mission = build_mission()
        link.corrupt_next_ack()

        first = await link.upload_mission(DRONE_ID, mission)
        second = await link.upload_mission(DRONE_ID, mission)

        assert first.checksum != mission.checksum
        assert second.checksum == mission.checksum


class TestStart:
    async def test_a_valid_authorisation_starts_the_mission(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        await link.upload_mission(DRONE_ID, build_flight(FlightStatus.MISSION_UPLOADED).mission)

        await link.start_mission(DRONE_ID, authorization(clock))

        assert link.operations("start") == [DRONE_ID]

    async def test_it_refuses_anything_that_is_not_an_authorisation(
        self, link: InMemoryDroneLink
    ) -> None:
        await link.upload_mission(DRONE_ID, build_mission())

        with pytest.raises(CommandRejectedError):
            await link.start_mission(DRONE_ID, object())  # type: ignore[arg-type]

        assert link.operations("start") == []

    async def test_it_refuses_an_authorisation_for_another_drone(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        await link.upload_mission(OTHER_DRONE, build_flight(FlightStatus.MISSION_UPLOADED).mission)

        with pytest.raises(CommandRejectedError, match="different drone"):
            await link.start_mission(OTHER_DRONE, authorization(clock))

    async def test_it_refuses_an_expired_authorisation(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        await link.upload_mission(DRONE_ID, build_flight(FlightStatus.MISSION_UPLOADED).mission)
        token = authorization(clock)
        clock.advance(seconds=(token.valid_until - clock()).total_seconds())  # exactly at expiry

        with pytest.raises(CommandRejectedError, match="expired"):
            await link.start_mission(DRONE_ID, token)

    async def test_it_accepts_an_authorisation_a_moment_before_it_expires(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        await link.upload_mission(DRONE_ID, build_flight(FlightStatus.MISSION_UPLOADED).mission)
        token = authorization(clock)
        clock.now = token.valid_until - timedelta(seconds=1)

        await link.start_mission(DRONE_ID, token)

        assert link.operations("start") == [DRONE_ID]

    async def test_it_refuses_when_no_mission_is_on_board(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        with pytest.raises(CommandRejectedError, match="not the authorised one"):
            await link.start_mission(DRONE_ID, authorization(clock))

    async def test_it_refuses_when_the_mission_on_board_is_not_the_authorised_one(
        self, link: InMemoryDroneLink, clock: FakeClock
    ) -> None:
        await link.upload_mission(DRONE_ID, build_mission(mission_id=UUID(int=0x99)))

        with pytest.raises(CommandRejectedError, match="not the authorised one"):
            await link.start_mission(DRONE_ID, authorization(clock))

        assert link.operations("start") == []


class TestTheSafetyCommands:
    @pytest.mark.parametrize("operation", ["pause_mission", "return_to_home", "land"])
    async def test_they_need_no_authorisation_and_no_mission(
        self, link: InMemoryDroneLink, operation: str
    ) -> None:
        await getattr(link, operation)(DRONE_ID)

        assert link.commands == [(operation.replace("_mission", ""), DRONE_ID)]


class TestFailureInjection:
    @pytest.mark.parametrize("operation", ["pause", "return_to_home", "land", "upload"])
    async def test_a_failure_stops_the_operation_until_cleared(
        self, link: InMemoryDroneLink, operation: str
    ) -> None:
        link.set_failure(operation, NoAcknowledgementError("down"))  # type: ignore[arg-type]
        call = {
            "pause": lambda: link.pause_mission(DRONE_ID),
            "return_to_home": lambda: link.return_to_home(DRONE_ID),
            "land": lambda: link.land(DRONE_ID),
            "upload": lambda: link.upload_mission(DRONE_ID, build_mission()),
        }[operation]

        with pytest.raises(NoAcknowledgementError):
            await call()
        assert link.commands == []  # it never reached the drone

        link.set_failure(operation, None)  # type: ignore[arg-type]
        await call()
        assert [op for op, _ in link.commands] == [operation]

    async def test_an_executed_failure_reaches_the_drone_but_loses_the_acknowledgement(
        self, link: InMemoryDroneLink
    ) -> None:
        link.set_failure("return_to_home", NoAcknowledgementError("lost"), executed=True)

        with pytest.raises(NoAcknowledgementError):
            await link.return_to_home(DRONE_ID)

        assert link.operations("return_to_home") == [DRONE_ID]

    async def test_a_failure_on_one_operation_does_not_affect_the_others(
        self, link: InMemoryDroneLink
    ) -> None:
        link.set_failure("land", CommandRejectedError("no"))

        await link.return_to_home(DRONE_ID)
        await link.pause_mission(DRONE_ID)

        assert [op for op, _ in link.commands] == ["return_to_home", "pause"]
