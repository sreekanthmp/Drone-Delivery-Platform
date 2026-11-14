"""Record telemetry samples: keep each drone's latest state live, and its battery honest.

For every valid sample from a registered drone it (1) stores the latest state in the live
view, (2) broadcasts it to watchers, and (3) keeps the *stored* battery level in step with the
drone's, in coarse steps, because drone selection reads the stored level. It never writes a
status, never sends anything to a drone, and never raises.

Samples are best-effort (QoS 0). The live view being down costs live tracking and nothing
else: the battery sync and the rest of the platform do not depend on it.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import structlog

from app.dispatch.service import DispatchService
from app.protocol.messages import ProtocolError, decode_telemetry
from app.realtime import channels
from app.realtime.models import DroneState, Envelope
from app.realtime.ports import Broadcast, StateStore

logger = structlog.get_logger(__name__)

_UNKNOWN_DRONE_CACHE_SECONDS = 60.0
_MAX_UNKNOWN_REMEMBERED = 1000


class SampleOutcome(StrEnum):
    RECORDED = "recorded"
    INVALID = "invalid"
    UNKNOWN_DRONE = "unknown_drone"
    """Not a registered drone: ignored, so strangers cannot fill the live view."""

    LIVE_VIEW_DOWN = "live_view_down"
    """The sample could not be stored live (the battery sync was still attempted)."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TelemetryRecorder:
    def __init__(
        self,
        dispatch: DispatchService,
        state: StateStore,
        broadcast: Broadcast,
        *,
        battery_step_percent: float = 5.0,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatch = dispatch
        self._state = state
        self._broadcast = broadcast
        self._step = battery_step_percent
        self._clock = clock
        self._monotonic = monotonic
        self._known: set[UUID] = set()
        self._unknown_until: dict[UUID, float] = {}
        self._synced_battery: dict[UUID, float] = {}
        self.counts: Counter[SampleOutcome] = Counter()

    async def handle_sample(self, drone_id: UUID, payload: bytes) -> SampleOutcome:
        """Process one message from ``drones/{drone_id}/telemetry``. Never raises."""
        outcome = await self._handle(drone_id, payload)
        self.counts[outcome] += 1
        return outcome

    async def _handle(self, drone_id: UUID, payload: bytes) -> SampleOutcome:
        try:
            sample = decode_telemetry(payload)
        except ProtocolError as error:
            logger.warning("sample_invalid", drone_id=str(drone_id), reason=str(error))
            return SampleOutcome.INVALID
        if not await self._is_registered(drone_id):
            return SampleOutcome.UNKNOWN_DRONE

        state = DroneState.from_telemetry(drone_id, sample, received_at=self._clock())
        outcome = SampleOutcome.RECORDED
        try:
            await self._state.set_state(state)
        except Exception as error:
            logger.warning("live_state_failed", drone_id=str(drone_id), error=repr(error))
            outcome = SampleOutcome.LIVE_VIEW_DOWN
        else:
            envelope = Envelope(
                kind="drone_state", droppable=True, data=state.model_dump(mode="json")
            )
            await self._broadcast.publish(channels.drone_channel(drone_id), envelope)
            await self._broadcast.publish(channels.FLEET, envelope)

        await self._sync_battery(drone_id, sample.battery_percent)
        return outcome

    async def _is_registered(self, drone_id: UUID) -> bool:
        if drone_id in self._known:
            return True
        now = self._monotonic()
        if self._unknown_until.get(drone_id, 0.0) > now:
            return False
        try:
            exists = await self._dispatch.drone_exists(drone_id)
        except Exception as error:
            logger.warning("drone_lookup_failed", drone_id=str(drone_id), error=repr(error))
            return False
        if exists:
            self._known.add(drone_id)
            return True
        if len(self._unknown_until) >= _MAX_UNKNOWN_REMEMBERED:
            self._unknown_until.clear()  # a flood of strangers must not grow memory either
        self._unknown_until[drone_id] = now + _UNKNOWN_DRONE_CACHE_SECONDS
        logger.warning("sample_from_unknown_drone", drone_id=str(drone_id))
        return False

    async def _sync_battery(self, drone_id: UUID, battery_percent: float) -> None:
        last = self._synced_battery.get(drone_id)
        if last is not None and abs(battery_percent - last) < self._step:
            return
        try:
            await self._dispatch.record_battery(drone_id, round(battery_percent, 1))
        except Exception as error:
            logger.warning("battery_sync_failed", drone_id=str(drone_id), error=repr(error))
            return
        self._synced_battery[drone_id] = battery_percent
