"""The messages that cross the MQTT wire, as validated models.

Rule 1 is visible in the shape of this module: the platform can send exactly five kinds of
command (``COMMAND_TYPES``), and none has a field that could carry a position, heading,
speed or status. A mission is data the drone flies on its own; a start carries the Rule 2
authorisation so the drone itself can refuse an expired or mismatched one.

Everything is strict (unknown fields are errors) and every timestamp must be timezone-aware.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.domain.flight_state_machine import StartAuthorization
from app.domain.models import Mission


class ProtocolError(ValueError):
    """A message is malformed, or is not one the protocol defines."""


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- mission and authorisation (data inside commands) ------------------------------------------


class WireWaypoint(_Message):
    kind: str
    latitude: float
    longitude: float
    altitude_m: float


class WireMission(_Message):
    """A mission as it travels: the canonical form plus the checksum the drone must echo."""

    mission_id: UUID
    cruise_speed_mps: float
    estimated_duration_s: float
    waypoints: list[WireWaypoint]
    checksum: str

    @classmethod
    def from_domain(cls, mission: Mission) -> Self:
        return cls.model_validate({**mission.as_dict(), "checksum": mission.checksum})

    def to_domain(self) -> Mission:
        """Rebuild the mission (raises ``MissionValidationError`` if it is not a valid one)."""
        return Mission.from_dict(self.model_dump(mode="json", exclude={"checksum"}))


class WireAuthorization(_Message):
    """The Rule 2 start authorisation, as sent to the drone."""

    flight_id: UUID
    drone_id: UUID
    permission_id: str
    mission_checksum: str
    authorized_at: AwareDatetime
    valid_until: AwareDatetime

    @classmethod
    def from_domain(cls, authorization: StartAuthorization) -> Self:
        return cls(
            flight_id=authorization.flight_id,
            drone_id=authorization.drone_id,
            permission_id=authorization.permission_id,
            mission_checksum=authorization.mission_checksum,
            authorized_at=authorization.authorized_at,
            valid_until=authorization.valid_until,
        )


# --- commands: cloud -> drone ------------------------------------------------------------------


class _Command(_Message):
    command_id: UUID
    drone_id: UUID
    sent_at: AwareDatetime
    reply_to: str
    """The topic the drone must publish its acknowledgement to."""


class UploadMission(_Command):
    type: Literal["upload_mission"] = "upload_mission"
    mission: WireMission


class StartMission(_Command):
    type: Literal["start_mission"] = "start_mission"
    authorization: WireAuthorization


class PauseMission(_Command):
    type: Literal["pause_mission"] = "pause_mission"


class ReturnToHome(_Command):
    type: Literal["return_to_home"] = "return_to_home"


class Land(_Command):
    type: Literal["land"] = "land"


AnyCommand = UploadMission | StartMission | PauseMission | ReturnToHome | Land

Command = Annotated[AnyCommand, Field(discriminator="type")]

COMMAND_TYPES: Final = frozenset(
    {"upload_mission", "start_mission", "pause_mission", "return_to_home", "land"}
)
"""Rule 1: the only things the platform can tell a drone to do."""

_COMMANDS: Final = TypeAdapter(Command)


# --- acknowledgements: drone -> cloud ----------------------------------------------------------


class AckStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AckMessage(_Message):
    command_id: UUID
    drone_id: UUID
    status: AckStatus
    acked_at: AwareDatetime
    reason: str | None = Field(default=None, max_length=500)
    mission_checksum: str | None = None
    """For an upload: the checksum of the mission the drone now holds (computed by the drone)."""


# --- events: drone -> cloud --------------------------------------------------------------------


class EventType(StrEnum):
    PACKAGE_PICKED_UP = "package_picked_up"
    DEPARTED_PICKUP = "departed_pickup"
    ARRIVING_AT_DROPOFF = "arriving_at_dropoff"
    PACKAGE_DELIVERED = "package_delivered"
    MISSION_COMPLETED = "mission_completed"
    DOCKED = "docked"
    FAULT = "fault"


class DroneEvent(_Message):
    event_id: UUID
    flight_id: UUID
    seq: int = Field(ge=1)
    """Counts the events of one flight from 1. A repeat or an older one is not applied again."""

    type: EventType
    occurred_at: AwareDatetime
    detail: dict[str, Any] = Field(default_factory=dict)


# --- encoding ----------------------------------------------------------------------------------


def encode(message: _Message) -> bytes:
    return message.model_dump_json().encode("utf-8")


def decode_command(payload: bytes) -> AnyCommand:
    try:
        return _COMMANDS.validate_json(payload)
    except ValidationError as error:
        raise ProtocolError(f"not a valid command: {_summarise(error)}") from error


def decode_ack(payload: bytes) -> AckMessage:
    try:
        return AckMessage.model_validate_json(payload)
    except ValidationError as error:
        raise ProtocolError(f"not a valid acknowledgement: {_summarise(error)}") from error


def decode_event(payload: bytes) -> DroneEvent:
    try:
        return DroneEvent.model_validate_json(payload)
    except ValidationError as error:
        raise ProtocolError(f"not a valid event: {_summarise(error)}") from error


def _summarise(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "message"
    return f"{where}: {first['msg']}"
