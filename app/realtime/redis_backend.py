"""The live view on Redis: latest state with a TTL, standing alerts, pub/sub broadcast.

Redis is a view, not a source of truth: everything here may be lost and rebuilt. Reads raise
if Redis is unreachable (the caller decides how to degrade); ``publish`` never raises.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from app.realtime import channels
from app.realtime.models import Alert, AlertKind, DroneState, Envelope, alert_key

logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RedisRealtime:
    """``StateStore``, ``Broadcast`` and ``AlertBoard`` over a ``redis.asyncio`` client."""

    def __init__(
        self,
        client: Any,
        *,
        state_ttl: timedelta = timedelta(seconds=60),
        recent_max: int = 200,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._redis = client
        self._ttl_ms = int(state_ttl.total_seconds() * 1000)
        self._recent_max = recent_max
        self._clock = clock

    async def ping(self) -> None:
        await self._redis.ping()

    # --- StateStore --------------------------------------------------------------------------

    async def set_state(self, state: DroneState) -> None:
        await self._redis.set(
            channels.state_key(state.drone_id), state.model_dump_json(), px=self._ttl_ms
        )

    async def get_state(self, drone_id: UUID) -> DroneState | None:
        raw = await self._redis.get(channels.state_key(drone_id))
        return None if raw is None else DroneState.model_validate_json(raw)

    async def all_states(self) -> dict[UUID, DroneState]:
        keys = [key async for key in self._redis.scan_iter(match=f"{channels.STATE_KEY_PREFIX}*")]
        if not keys:
            return {}
        states: dict[UUID, DroneState] = {}
        for raw in await self._redis.mget(keys):
            if raw is not None:  # it may have expired between SCAN and MGET
                state = DroneState.model_validate_json(raw)
                states[state.drone_id] = state
        return states

    # --- Broadcast ---------------------------------------------------------------------------

    async def publish(self, channel: str, envelope: Envelope) -> None:
        try:
            await self._redis.publish(channel, envelope.encode())
        except Exception as error:
            logger.warning("broadcast_failed", channel=channel, error=repr(error))

    # --- AlertBoard --------------------------------------------------------------------------

    async def raise_alert(self, alert: Alert) -> bool:
        is_new = bool(
            await self._redis.hsetnx(channels.ACTIVE_ALERTS_KEY, alert.key, alert.model_dump_json())
        )
        if is_new:
            await self._announce(alert)
        return is_new

    async def resolve(self, kind: AlertKind, drone_id: UUID | None, *, at: datetime) -> bool:
        key = alert_key(kind, drone_id)
        raw = await self._redis.hget(channels.ACTIVE_ALERTS_KEY, key)
        if raw is None or not await self._redis.hdel(channels.ACTIVE_ALERTS_KEY, key):
            return False  # not active, or another process just cleared it
        await self._announce(Alert.model_validate_json(raw).resolved(at))
        return True

    async def notify(self, alert: Alert) -> None:
        await self._announce(alert)

    async def active(self) -> list[Alert]:
        values = await self._redis.hvals(channels.ACTIVE_ALERTS_KEY)
        return sorted((Alert.model_validate_json(v) for v in values), key=lambda a: a.raised_at)

    async def recent(self, limit: int = 50) -> list[Alert]:
        values = await self._redis.lrange(channels.RECENT_ALERTS_KEY, 0, max(limit, 1) - 1)
        return [Alert.model_validate_json(v) for v in values]

    async def _announce(self, alert: Alert) -> None:
        await self._redis.lpush(channels.RECENT_ALERTS_KEY, alert.model_dump_json())
        await self._redis.ltrim(channels.RECENT_ALERTS_KEY, 0, self._recent_max - 1)
        await self.publish(
            channels.FLEET,
            Envelope(kind="alert", droppable=False, data=alert.model_dump(mode="json")),
        )
