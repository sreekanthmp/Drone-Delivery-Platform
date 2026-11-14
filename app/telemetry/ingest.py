"""Decode a drone's event, drop repeats, and give it to the dispatch service.

This class has no MQTT client (so it cannot publish a command) and no store (so it cannot
write a status): it holds a ``DispatchService`` and nothing else. An architecture test keeps
it that way.

MQTT delivers at least once, so repeats are normal. Two layers absorb them: a per-flight
sequence number (a repeat or an older event is dropped here, cheaply) and the service's own
idempotency (an event whose effect is already recorded is "already applied", which also
covers a restart that forgot the sequence numbers).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import structlog

from app.dispatch.progress import ProgressEvent, ProgressResult
from app.dispatch.service import DispatchService
from app.protocol.messages import DroneEvent, ProtocolError, decode_event

logger = structlog.get_logger(__name__)

_MAX_DETAIL_CHARS = 2000
_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0, 4.0)
"""Back-off for a transient failure while recording (for example a database restart)."""


class IngestOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    NOTED = "noted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    """The sequence number was already seen for this flight: dropped without touching anything."""

    INVALID = "invalid"
    """Not a message the protocol defines."""

    FAILED = "failed"
    """Recording failed even after retries; the event is lost (the broker already acked it)."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    reason: str | None = None


def _sleep(seconds: float) -> Awaitable[None]:
    return asyncio.sleep(seconds)


class TelemetryIngest:
    def __init__(
        self,
        dispatch: DispatchService,
        *,
        max_tracked_flights: int = 10_000,
        retry_delays: tuple[float, ...] = _RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
    ) -> None:
        self._dispatch = dispatch
        self._last_seq: dict[UUID, int] = {}  # insertion-ordered: oldest first
        self._max_tracked = max_tracked_flights
        self._retry_delays = retry_delays
        self._sleep = sleep
        self.counts: Counter[IngestOutcome] = Counter()

    async def handle_event(self, drone_id: UUID, payload: bytes) -> IngestResult:
        """Process one message from ``drones/{drone_id}/events``. Never raises."""
        result = await self._handle(drone_id, payload)
        self.counts[result.outcome] += 1
        return result

    async def _handle(self, drone_id: UUID, payload: bytes) -> IngestResult:
        try:
            event = decode_event(payload)
        except ProtocolError as error:
            logger.warning("event_invalid", drone_id=str(drone_id), reason=str(error))
            return IngestResult(IngestOutcome.INVALID, str(error))

        last = self._last_seq.get(event.flight_id, 0)
        if event.seq <= last:
            logger.info(
                "event_duplicate",
                drone_id=str(drone_id),
                flight_id=str(event.flight_id),
                seq=event.seq,
                last_seq=last,
            )
            return IngestResult(IngestOutcome.DUPLICATE, f"seq {event.seq} <= {last}")
        if last and event.seq > last + 1:
            logger.warning(
                "event_gap",
                drone_id=str(drone_id),
                flight_id=str(event.flight_id),
                seq=event.seq,
                last_seq=last,
            )

        try:
            progress = await self._record(drone_id, event)
        except Exception as error:
            logger.error(
                "event_lost",
                drone_id=str(drone_id),
                flight_id=str(event.flight_id),
                seq=event.seq,
                error=repr(error),
            )
            return IngestResult(IngestOutcome.FAILED, repr(error))

        self._remember(event.flight_id, event.seq)
        return IngestResult(IngestOutcome(progress.outcome.value), progress.reason)

    async def _record(self, drone_id: UUID, event: DroneEvent) -> ProgressResult:
        """Call the service, retrying transient failures (it never raises for a bad event)."""
        for delay in (*self._retry_delays, None):
            try:
                return await self._dispatch.record_progress(
                    drone_id,
                    event.flight_id,
                    ProgressEvent(event.type.value),
                    occurred_at=event.occurred_at,
                    detail=_bounded(event.detail),
                )
            except Exception as error:
                if delay is None:
                    raise
                logger.warning(
                    "event_retry", flight_id=str(event.flight_id), error=repr(error), delay=delay
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _remember(self, flight_id: UUID, seq: int) -> None:
        self._last_seq.pop(flight_id, None)  # re-insert so the most recent flight is last
        self._last_seq[flight_id] = seq
        while len(self._last_seq) > self._max_tracked:
            del self._last_seq[next(iter(self._last_seq))]


def _bounded(detail: dict[str, Any]) -> dict[str, Any]:
    """A drone's free-form detail, cut to a size that is safe to log."""
    try:
        if len(json.dumps(detail, default=str)) <= _MAX_DETAIL_CHARS:
            return detail
    except (TypeError, ValueError):
        pass
    return {"truncated": True}
