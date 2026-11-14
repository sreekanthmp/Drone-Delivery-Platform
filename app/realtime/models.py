"""Values kept in the live view: a drone's latest sample, and alerts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.protocol.messages import DronePhase, Telemetry


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DroneState(_Model):
    """The latest sample from one drone, stamped with when the platform received it."""

    drone_id: UUID
    latitude: float
    longitude: float
    altitude_m: float
    battery_percent: float
    phase: DronePhase
    flight_id: UUID | None
    sent_at: AwareDatetime
    """The drone's own clock: good for ordering, never for judging freshness."""

    received_at: AwareDatetime
    """The platform's clock when the sample arrived: this is what staleness is judged by."""

    @classmethod
    def from_telemetry(
        cls, drone_id: UUID, sample: Telemetry, *, received_at: AwareDatetime
    ) -> Self:
        return cls(
            drone_id=drone_id,
            latitude=sample.latitude,
            longitude=sample.longitude,
            altitude_m=sample.altitude_m,
            battery_percent=sample.battery_percent,
            phase=sample.phase,
            flight_id=sample.flight_id,
            sent_at=sample.sent_at,
            received_at=received_at,
        )


class AlertKind(StrEnum):
    LINK_LOST = "link_lost"
    """A drone with a flight in the air has stopped sending samples."""

    LOW_BATTERY = "low_battery"
    STATE_MISMATCH = "state_mismatch"
    """The platform and the drone disagree about whether it is flying."""

    FAULT = "fault"
    """The drone reported a fault (a one-off: it is never "resolved")."""

    STALE_RESERVATION = "stale_reservation"
    """The reaper cancelled a flight that a dispatch left half-finished (a one-off)."""


class Alert(_Model):
    kind: AlertKind
    drone_id: UUID | None
    message: str
    raised_at: AwareDatetime
    flight_id: UUID | None = None
    order_id: UUID | None = None
    resolved_at: AwareDatetime | None = None

    @property
    def key(self) -> str:
        return alert_key(self.kind, self.drone_id)

    def resolved(self, at: AwareDatetime) -> Alert:
        return self.model_copy(update={"resolved_at": at})


def alert_key(kind: AlertKind, drone_id: UUID | None) -> str:
    return f"{kind.value}:{drone_id or '-'}"


class Envelope(_Model):
    """What travels on a live channel. ``droppable`` tells a slow consumer what it may skip."""

    kind: Literal["drone_state", "audit", "alert"]
    droppable: bool
    data: dict[str, Any]

    def encode(self) -> str:
        return self.model_dump_json()

    @classmethod
    def decode(cls, raw: str | bytes) -> Self:
        return cls.model_validate_json(raw)
