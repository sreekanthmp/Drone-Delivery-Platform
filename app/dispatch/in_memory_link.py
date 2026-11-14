"""An in-memory drone link for development and tests. NOT a real drone connection.

It behaves like a well-mannered drone: it acknowledges uploads with the checksum of what
it received, and it checks the Rule 2 authorisation before it will start (right type,
right drone, not expired, and the mission on board is the one that was authorised).

For tests it can be told to fail an operation (see :meth:`set_failure`) or to echo a wrong
checksum, and it records every operation in :attr:`commands`. The MQTT link replaces it
in the telemetry phase.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.dispatch.link import (
    CommandRejectedError,
    DroneLinkError,
    MissionAck,
)
from app.domain.flight_state_machine import StartAuthorization
from app.domain.models import Mission

Operation = Literal["upload", "start", "pause", "return_to_home", "land"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryDroneLink:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._failures: dict[Operation, tuple[DroneLinkError, bool]] = {}
        self._corrupt_next_ack = False
        self.on_board: dict[UUID, Mission] = {}
        """The mission each drone currently holds."""

        self.commands: list[tuple[Operation, UUID]] = []
        """Every operation that reached a drone, oldest first."""

    # --- controls for tests and development (not part of the DroneLink port) ---------------

    def set_failure(
        self, operation: Operation, error: DroneLinkError | None, *, executed: bool = False
    ) -> None:
        """Make ``operation`` raise ``error`` until cleared with ``None``.

        With ``executed=True`` the drone acts but the acknowledgement is lost (the command is
        recorded, then the error raised): the case a no-answer error cannot distinguish.
        """
        if error is None:
            self._failures.pop(operation, None)
        else:
            self._failures[operation] = (error, executed)

    def corrupt_next_ack(self) -> None:
        """Make the next upload echo back a checksum that does not match."""
        self._corrupt_next_ack = True

    def operations(self, operation: Operation) -> list[UUID]:
        return [drone_id for op, drone_id in self.commands if op == operation]

    # --- DroneLink -------------------------------------------------------------------------

    async def upload_mission(self, drone_id: UUID, mission: Mission) -> MissionAck:
        self._fail_if_told("upload", drone_id)
        self.on_board[drone_id] = mission
        self.commands.append(("upload", drone_id))
        checksum = "0" * 64 if self._corrupt_next_ack else mission.checksum
        self._corrupt_next_ack = False
        return MissionAck(drone_id=drone_id, mission_id=mission.mission_id, checksum=checksum)

    async def start_mission(self, drone_id: UUID, authorization: StartAuthorization) -> None:
        if not isinstance(authorization, StartAuthorization):
            raise CommandRejectedError("a start needs a StartAuthorization")
        if authorization.drone_id != drone_id:
            raise CommandRejectedError("the authorisation is for a different drone")
        if self._clock() >= authorization.valid_until:
            raise CommandRejectedError("the authorisation has expired")
        mission = self.on_board.get(drone_id)
        if mission is None or mission.checksum != authorization.mission_checksum:
            raise CommandRejectedError("the mission on board is not the authorised one")
        self._fail_if_told("start", drone_id)
        self.commands.append(("start", drone_id))

    async def pause_mission(self, drone_id: UUID) -> None:
        self._fail_if_told("pause", drone_id)
        self.commands.append(("pause", drone_id))

    async def return_to_home(self, drone_id: UUID) -> None:
        self._fail_if_told("return_to_home", drone_id)
        self.commands.append(("return_to_home", drone_id))

    async def land(self, drone_id: UUID) -> None:
        self._fail_if_told("land", drone_id)
        self.commands.append(("land", drone_id))

    def _fail_if_told(self, operation: Operation, drone_id: UUID) -> None:
        failure = self._failures.get(operation)
        if failure is None:
            return
        error, executed = failure
        if executed:
            self.commands.append((operation, drone_id))
        raise error
