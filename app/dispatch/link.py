"""The drone link: the whole surface through which the cloud touches a drone (Rule 1).

**Five operations, and only five.** The cloud uploads a predefined mission and sends
high-level commands; the drone's own flight controller flies. There is deliberately no
operation to steer, reposition, or set a speed, heading or attitude, and
``tests/unit/test_rule1_no_steering.py`` fails if one is ever added.

``start_mission`` needs a :class:`StartAuthorization`, which only the flight state
machine can create after the Rule 2 permission gate has passed. Implementations should
re-check it (the in-memory link does) and may carry its permission id to the drone.

Two failure kinds matter to callers, so they are separate:

* :class:`CommandRejectedError`: the drone refused. It definitely did not act.
* :class:`NoAcknowledgementError`: no answer. The drone may or may not have acted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.flight_state_machine import StartAuthorization
from app.domain.models import Mission


class DroneLinkError(Exception):
    """Base class for every way the link can fail."""


class CommandRejectedError(DroneLinkError):
    """The drone refused the command or the mission. It did not act."""


class NoAcknowledgementError(DroneLinkError):
    """No answer arrived in time. The drone may or may not have acted."""


@dataclass(frozen=True, slots=True)
class MissionAck:
    """What the drone says it now holds. ``checksum`` must equal the mission's own."""

    drone_id: UUID
    mission_id: UUID
    checksum: str


class DroneLink(Protocol):
    async def upload_mission(self, drone_id: UUID, mission: Mission) -> MissionAck:
        """Load ``mission`` onto the drone. The drone echoes back what it received."""
        ...

    async def start_mission(self, drone_id: UUID, authorization: StartAuthorization) -> None:
        """Start (or resume) the uploaded mission. Needs the Rule 2 authorisation."""
        ...

    async def pause_mission(self, drone_id: UUID) -> None:
        """Pause the mission in progress."""
        ...

    async def return_to_home(self, drone_id: UUID) -> None:
        """Abandon the mission and fly home. The drone does this on its own."""
        ...

    async def land(self, drone_id: UUID) -> None:
        """Land immediately where the drone is."""
        ...
