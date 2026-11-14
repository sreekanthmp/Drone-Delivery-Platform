"""What the rest of the platform needs from the live view (so it can be faked in tests)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.realtime.models import Alert, AlertKind, DroneState, Envelope


class StateStore(Protocol):
    async def set_state(self, state: DroneState) -> None:
        """Remember the drone's latest sample (it expires if the drone goes silent)."""
        ...

    async def get_state(self, drone_id: UUID) -> DroneState | None: ...

    async def all_states(self) -> dict[UUID, DroneState]:
        """The latest sample of every drone that has been heard from recently."""
        ...


class Broadcast(Protocol):
    async def publish(self, channel: str, envelope: Envelope) -> None:
        """Send to everyone watching ``channel``. **Never raises**: a view must not break
        the thing it is viewing."""
        ...


class AlertBoard(Protocol):
    async def raise_alert(self, alert: Alert) -> bool:
        """Raise a standing alert. ``True`` if it is new (it was not already active)."""
        ...

    async def resolve(self, kind: AlertKind, drone_id: UUID | None, *, at: datetime) -> bool:
        """Clear a standing alert. ``True`` if it was active."""
        ...

    async def notify(self, alert: Alert) -> None:
        """Record a one-off alert (a fault, a reaped reservation): it is never active."""
        ...

    async def active(self) -> list[Alert]: ...

    async def recent(self, limit: int = 50) -> list[Alert]:
        """The latest alerts raised or resolved, newest first."""
        ...


class LiveView(StateStore, Broadcast, AlertBoard, Protocol):
    """Everything the live view offers, in one object (Redis, or the in-memory stand-in)."""
