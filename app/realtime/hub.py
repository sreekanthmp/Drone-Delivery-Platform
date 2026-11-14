"""Fans live messages out to the connections watching them.

One Redis subscription per process (a pattern, ``ch:*``) feeds this hub, which routes each
message to the subscriptions that asked for its channel. Each subscription has a **bounded**
queue, so one slow watcher can never use unbounded memory or slow anyone else down.

Backpressure policy (what happens when a watcher falls behind and its queue is full):

* a **droppable** message (a position sample, which is superseded by the next one) is dropped,
  oldest first;
* a message that is **not** droppable (a status change, an alert) is never dropped: it takes
  the place of the oldest droppable one. If the queue holds only undroppable messages the
  watcher is hopelessly behind, so the subscription is *interrupted* and the consumer is told
  to close the connection: the client reconnects and re-syncs from a fresh snapshot rather than
  being left with a silently wrong picture.

The same happens when the Redis feed itself is interrupted (messages may have been missed).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Protocol, Self

import structlog
from pydantic import ValidationError

from app.realtime import channels
from app.realtime.models import Envelope

logger = structlog.get_logger(__name__)

DEFAULT_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)


class SubscriptionInterrupted(Exception):
    """The watcher can no longer be given a faithful picture; it must re-sync."""


class PubSubSource(Protocol):
    def pubsub(self) -> Any: ...


class Subscription:
    def __init__(self, hub: Hub, channel_names: Iterable[str], max_queue: int) -> None:
        self._hub = hub
        self.channels: set[str] = set(channel_names)
        self._max = max_queue
        self._queue: deque[tuple[str, Envelope]] = deque()
        self._wake = asyncio.Event()
        self._reason: str | None = None
        self.dropped = 0
        """How many droppable messages this watcher has lost to its own slowness."""

    # --- consumer side -----------------------------------------------------------------------

    async def get(self, timeout: float | None = None) -> tuple[str, Envelope] | None:
        """The next message, ``None`` if none arrived within ``timeout``.

        Raises :class:`SubscriptionInterrupted` when the watcher must re-sync.
        """
        while True:
            if self._reason is not None:
                raise SubscriptionInterrupted(self._reason)
            if self._queue:
                return self._queue.popleft()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                return None

    def add_channel(self, channel: str) -> None:
        """Also receive ``channel`` (a stream learns which drone it follows as it goes)."""
        self._hub._add(self, channel)

    def close(self) -> None:
        self._hub._remove(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def interrupted(self) -> bool:
        return self._reason is not None

    # --- hub side ----------------------------------------------------------------------------

    def _offer(self, channel: str, envelope: Envelope) -> None:
        if self._reason is not None:
            return
        if len(self._queue) >= self._max:
            oldest_droppable = next(
                (i for i, (_, queued) in enumerate(self._queue) if queued.droppable), None
            )
            if oldest_droppable is not None:
                del self._queue[oldest_droppable]
                self.dropped += 1
            elif envelope.droppable:
                self.dropped += 1
                return  # nothing droppable to make room with: this one is the casualty
            else:
                self._interrupt("the watcher cannot keep up with the changes")
                return
        self._queue.append((channel, envelope))
        self._wake.set()

    def _interrupt(self, reason: str) -> None:
        self._reason = reason
        self._queue.clear()
        self._wake.set()


class Hub:
    def __init__(self, *, max_queue: int = 100) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be at least 1")
        self._max_queue = max_queue
        self._by_channel: dict[str, set[Subscription]] = {}
        self.connected = False
        """Whether there is a live feed at all. Streams refuse to start without one."""

    def subscribe(self, channel_names: Iterable[str]) -> Subscription:
        subscription = Subscription(self, [], self._max_queue)
        for channel in channel_names:
            self._add(subscription, channel)
        return subscription

    def route(self, channel: str, envelope: Envelope) -> None:
        """Deliver a message to everyone watching its channel."""
        for subscription in list(self._by_channel.get(channel, ())):
            subscription._offer(channel, envelope)

    def interrupt_all(self, reason: str) -> None:
        """Tell every watcher the feed has a gap, so each re-syncs."""
        for subscription in {s for subs in self._by_channel.values() for s in subs}:
            subscription._interrupt(reason)

    @property
    def watchers(self) -> int:
        return len({s for subs in self._by_channel.values() for s in subs})

    def attach_memory(self, listeners: list[Callable[[str, Envelope], None]]) -> None:
        """Feed the hub from an in-memory live view (tests, and processes without Redis)."""
        listeners.append(self.route)
        self.connected = True

    async def run_redis(
        self,
        source: PubSubSource,
        *,
        reconnect_delays: tuple[float, ...] = DEFAULT_RECONNECT_DELAYS,
    ) -> None:
        """Read the live feed from Redis forever, reconnecting as needed."""
        attempt = 0
        while True:
            pubsub = source.pubsub()
            try:
                await pubsub.psubscribe(channels.PATTERN)
                self.connected = True
                attempt = 0
                logger.info("live_feed_connected")
                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue
                    try:
                        envelope = Envelope.decode(message["data"])
                    except ValidationError:
                        logger.warning("live_message_invalid", channel=message["channel"])
                        continue
                    self.route(message["channel"], envelope)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("live_feed_lost", error=repr(error))
            finally:
                self.connected = False
                try:
                    await pubsub.aclose()
                except Exception:
                    logger.debug("live_feed_close_failed")
            # Whatever was published while we were away is gone: every watcher must re-sync.
            self.interrupt_all("the live feed was interrupted")
            await asyncio.sleep(reconnect_delays[min(attempt, len(reconnect_delays) - 1)])
            attempt += 1

    # --- subscription bookkeeping ------------------------------------------------------------

    def _add(self, subscription: Subscription, channel: str) -> None:
        subscription.channels.add(channel)
        self._by_channel.setdefault(channel, set()).add(subscription)

    def _remove(self, subscription: Subscription) -> None:
        for channel in list(subscription.channels):
            watchers = self._by_channel.get(channel)
            if watchers is not None:
                watchers.discard(subscription)
                if not watchers:
                    del self._by_channel[channel]
        subscription.channels.clear()
