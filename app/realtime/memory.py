"""An in-memory live view: for tests, and for a process that has no Redis.

It behaves like the Redis one (latest state that expires, standing alerts, a recent list,
fire-and-forget broadcast) so the same tests can run against both.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.realtime import channels
from app.realtime.models import Alert, AlertKind, DroneState, Envelope, alert_key

logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryRealtime:
    """Implements ``StateStore``, ``Broadcast`` and ``AlertBoard`` together."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        state_ttl: timedelta = timedelta(seconds=60),
        recent_max: int = 200,
    ) -> None:
        self._clock = clock
        self._ttl = state_ttl
        self._recent_max = recent_max
        self._states: dict[UUID, DroneState] = {}
        self._active: dict[str, Alert] = {}
        self._recent: list[Alert] = []
        self.published: list[tuple[str, Envelope]] = []
        """Everything broadcast, oldest first (for tests to inspect)."""

        self.listeners: list[Callable[[str, Envelope], None]] = []
        """Called synchronously for every broadcast (a stand-in for a pub/sub subscriber)."""

        self.broken = False
        """Make every operation fail, as an unreachable Redis would."""

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("the live view is unavailable")

    # --- StateStore --------------------------------------------------------------------------

    async def set_state(self, state: DroneState) -> None:
        self._check()
        self._states[state.drone_id] = state

    async def get_state(self, drone_id: UUID) -> DroneState | None:
        self._check()
        state = self._states.get(drone_id)
        return state if state is not None and self._fresh(state) else None

    async def all_states(self) -> dict[UUID, DroneState]:
        self._check()
        return {i: s for i, s in self._states.items() if self._fresh(s)}

    def _fresh(self, state: DroneState) -> bool:
        return self._clock() - state.received_at < self._ttl

    # --- Broadcast ---------------------------------------------------------------------------

    async def publish(self, channel: str, envelope: Envelope) -> None:
        if self.broken:
            logger.warning("broadcast_failed", channel=channel)
            return
        self.published.append((channel, envelope))
        for listener in list(self.listeners):
            listener(channel, envelope)

    # --- AlertBoard --------------------------------------------------------------------------

    async def raise_alert(self, alert: Alert) -> bool:
        self._check()
        if alert.key in self._active:
            return False
        self._active[alert.key] = alert
        await self._announce(alert)
        return True

    async def resolve(self, kind: AlertKind, drone_id: UUID | None, *, at: datetime) -> bool:
        self._check()
        alert = self._active.pop(alert_key(kind, drone_id), None)
        if alert is None:
            return False
        await self._announce(alert.resolved(at))
        return True

    async def notify(self, alert: Alert) -> None:
        self._check()
        await self._announce(alert)

    async def active(self) -> list[Alert]:
        self._check()
        return list(self._active.values())

    async def recent(self, limit: int = 50) -> list[Alert]:
        self._check()
        return self._recent[:limit]

    async def _announce(self, alert: Alert) -> None:
        self._recent.insert(0, alert)
        del self._recent[self._recent_max :]
        envelope = Envelope(kind="alert", droppable=False, data=alert.model_dump(mode="json"))
        await self.publish(channels.FLEET, envelope)
