"""The hub: routing, bounded queues, the backpressure policy, and the Redis reader."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.realtime.hub import Hub, SubscriptionInterrupted
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Envelope


def position(n: int) -> Envelope:
    return Envelope(kind="drone_state", droppable=True, data={"n": n})


def change(n: int) -> Envelope:
    return Envelope(kind="audit", droppable=False, data={"n": n})


async def drain(subscription: Any) -> list[int]:
    seen = []
    while (message := await subscription.get(timeout=0.01)) is not None:
        seen.append(message[1].data["n"])
    return seen


class TestRouting:
    async def test_a_message_goes_only_to_those_watching_its_channel(self) -> None:
        hub = Hub()
        a = hub.subscribe(["ch:one"])
        b = hub.subscribe(["ch:two"])
        both = hub.subscribe(["ch:one", "ch:two"])

        hub.route("ch:one", change(1))
        hub.route("ch:two", change(2))

        assert await drain(a) == [1]
        assert await drain(b) == [2]
        assert await drain(both) == [1, 2]

    async def test_the_message_arrives_with_its_channel(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])

        hub.route("ch:x", change(7))

        channel, envelope = await sub.get(timeout=1) or ("", change(0))
        assert channel == "ch:x"
        assert envelope.data == {"n": 7}

    async def test_order_is_preserved(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])

        for n in range(20):
            hub.route("ch:x", change(n))

        assert await drain(sub) == list(range(20))

    async def test_nothing_for_nobody(self) -> None:
        Hub().route("ch:unwatched", change(1))  # must not raise

    async def test_a_channel_can_be_added_later(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:order"])
        hub.route("ch:drone", position(1))  # not watching it yet

        sub.add_channel("ch:drone")
        hub.route("ch:drone", position(2))

        assert await drain(sub) == [2]

    async def test_closing_stops_delivery_and_forgets_the_watcher(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])
        assert hub.watchers == 1

        sub.close()
        hub.route("ch:x", change(1))

        assert hub.watchers == 0
        assert await sub.get(timeout=0.01) is None

    async def test_the_context_manager_closes(self) -> None:
        hub = Hub()

        with hub.subscribe(["ch:x"]):
            assert hub.watchers == 1

        assert hub.watchers == 0

    async def test_a_wait_times_out_with_none(self) -> None:
        sub = Hub().subscribe(["ch:x"])

        assert await sub.get(timeout=0.02) is None

    async def test_a_waiting_consumer_is_woken_by_a_message(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])
        waiting = asyncio.create_task(sub.get(timeout=5))
        await asyncio.sleep(0.01)

        hub.route("ch:x", change(9))

        message = await asyncio.wait_for(waiting, 1)
        assert message is not None
        assert message[1].data == {"n": 9}


class TestBackpressure:
    async def test_below_the_limit_nothing_is_lost(self) -> None:
        hub = Hub(max_queue=5)
        sub = hub.subscribe(["ch:x"])

        for n in range(5):
            hub.route("ch:x", position(n))

        assert await drain(sub) == [0, 1, 2, 3, 4]
        assert sub.dropped == 0

    async def test_a_full_queue_drops_the_oldest_position_first(self) -> None:
        hub = Hub(max_queue=3)
        sub = hub.subscribe(["ch:x"])

        for n in range(6):
            hub.route("ch:x", position(n))

        assert await drain(sub) == [3, 4, 5]  # the newest three: positions are superseded
        assert sub.dropped == 3

    async def test_a_change_displaces_the_oldest_position_and_is_never_dropped(self) -> None:
        hub = Hub(max_queue=3)
        sub = hub.subscribe(["ch:x"])
        for n in range(3):
            hub.route("ch:x", position(n))

        hub.route("ch:x", change(100))

        assert await drain(sub) == [1, 2, 100]
        assert sub.dropped == 1

    async def test_changes_survive_a_flood_of_positions(self) -> None:
        hub = Hub(max_queue=4)
        sub = hub.subscribe(["ch:x"])
        hub.route("ch:x", change(1))
        for n in range(100):
            hub.route("ch:x", position(1000 + n))
        hub.route("ch:x", change(2))

        seen = await drain(sub)

        assert 1 in seen
        assert 2 in seen
        assert seen.index(1) < seen.index(2)  # their order is intact
        assert len(seen) == 4

    async def test_a_position_arriving_when_only_changes_are_queued_is_the_casualty(self) -> None:
        hub = Hub(max_queue=2)
        sub = hub.subscribe(["ch:x"])
        hub.route("ch:x", change(1))
        hub.route("ch:x", change(2))

        hub.route("ch:x", position(3))

        assert not sub.interrupted
        assert await drain(sub) == [1, 2]
        assert sub.dropped == 1

    async def test_changes_the_watcher_cannot_keep_up_with_interrupt_it(self) -> None:
        hub = Hub(max_queue=2)
        sub = hub.subscribe(["ch:x"])
        hub.route("ch:x", change(1))
        hub.route("ch:x", change(2))

        hub.route("ch:x", change(3))  # nothing droppable and no room: it must re-sync

        assert sub.interrupted
        with pytest.raises(SubscriptionInterrupted, match="keep up"):
            await sub.get(timeout=0.01)

    async def test_an_interrupted_watcher_gets_nothing_more_and_others_are_unaffected(self) -> None:
        hub = Hub(max_queue=2)
        slow = hub.subscribe(["ch:x"])  # never reads
        quick = hub.subscribe(["ch:x"])  # reads everything as it arrives
        received = []

        for n in (1, 2, 3):
            hub.route("ch:x", change(n))
            received += await drain(quick)

        assert received == [1, 2, 3]
        assert slow.interrupted  # it fell three changes behind with room for two
        assert not quick.interrupted
        with pytest.raises(SubscriptionInterrupted):
            await slow.get(timeout=0.01)
        hub.route("ch:x", change(4))
        assert await drain(quick) == [4]  # and the rest carry on

    async def test_one_slow_watcher_never_blocks_routing_to_the_rest(self) -> None:
        hub = Hub(max_queue=2)
        slow = hub.subscribe(["ch:x"])  # never read
        others = [hub.subscribe(["ch:x"]) for _ in range(3)]

        for n in range(50):
            hub.route("ch:x", position(n))
            for other in others:
                await other.get(timeout=0.01)  # they keep up

        assert slow.dropped > 0
        assert all(o.dropped == 0 for o in others)

    async def test_interrupt_all_tells_every_watcher_to_resync(self) -> None:
        hub = Hub()
        a, b = hub.subscribe(["ch:a"]), hub.subscribe(["ch:b"])

        hub.interrupt_all("the live feed was interrupted")

        for sub in (a, b):
            with pytest.raises(SubscriptionInterrupted, match="interrupted"):
                await sub.get(timeout=0.01)

    def test_the_queue_must_hold_something(self) -> None:
        with pytest.raises(ValueError, match="max_queue"):
            Hub(max_queue=0)


class TestInMemoryFeed:
    async def test_broadcasts_reach_watchers(self) -> None:
        live = InMemoryRealtime()
        hub = Hub()
        hub.attach_memory(live.listeners)
        sub = hub.subscribe(["ch:x"])

        await live.publish("ch:x", change(1))
        await live.publish("ch:other", change(2))

        assert hub.connected
        assert await drain(sub) == [1]


class FakePubSub:
    """Stands in for a ``redis.asyncio`` pubsub: scripted messages, then a drop or a hold."""

    def __init__(self, script: list[Any], *, fail_to_subscribe: bool = False) -> None:
        self.script = script
        self.fail_to_subscribe = fail_to_subscribe
        self.patterns: list[str] = []
        self.closed = False

    async def psubscribe(self, pattern: str) -> None:
        if self.fail_to_subscribe:
            raise ConnectionError("redis is down")
        self.patterns.append(pattern)

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        for item in self.script:
            if isinstance(item, Exception):
                raise item
            yield item
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class FakeSource:
    def __init__(self, *pubsubs: FakePubSub) -> None:
        self._pubsubs = list(pubsubs)
        self.created: list[FakePubSub] = []

    def pubsub(self) -> FakePubSub:
        made = self._pubsubs.pop(0) if self._pubsubs else FakePubSub([])
        self.created.append(made)
        return made


def pmessage(channel: str, envelope: Envelope | str) -> dict[str, Any]:
    data = envelope if isinstance(envelope, str) else envelope.encode()
    return {"type": "pmessage", "pattern": "ch:*", "channel": channel, "data": data}


class TestRedisReader:
    async def test_it_subscribes_to_the_pattern_and_routes_messages(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])
        source = FakeSource(
            FakePubSub(
                [
                    {"type": "psubscribe", "pattern": "ch:*", "channel": "ch:*", "data": 1},
                    pmessage("ch:x", change(1)),
                    pmessage("ch:y", change(2)),
                    pmessage("ch:x", change(3)),
                ]
            )
        )
        task = asyncio.create_task(hub.run_redis(source))

        seen = []
        for _ in range(2):
            message = await sub.get(timeout=2)
            assert message is not None
            seen.append(message[1].data["n"])
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert seen == [1, 3]
        assert source.created[0].patterns == ["ch:*"]

    async def test_a_garbled_message_is_skipped_not_fatal(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])
        source = FakeSource(FakePubSub([pmessage("ch:x", "not json"), pmessage("ch:x", change(5))]))
        task = asyncio.create_task(hub.run_redis(source))

        message = await sub.get(timeout=2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert message is not None
        assert message[1].data == {"n": 5}

    async def test_a_lost_connection_interrupts_watchers_and_it_reconnects(self) -> None:
        hub = Hub()
        sub = hub.subscribe(["ch:x"])
        source = FakeSource(
            FakePubSub([pmessage("ch:x", change(1)), ConnectionError("connection lost")]),
            FakePubSub([pmessage("ch:x", change(2))]),
        )
        task = asyncio.create_task(hub.run_redis(source, reconnect_delays=(0.01,)))

        for _ in range(200):
            if len(source.created) >= 2 and hub.connected:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(source.created) >= 2  # it came back
        assert source.created[0].closed  # and cleaned up the dead connection
        with pytest.raises(SubscriptionInterrupted, match="interrupted"):
            await sub.get(timeout=0.01)  # the old watcher was told to re-sync

    async def test_a_new_watcher_after_the_reconnect_gets_the_new_messages(self) -> None:
        hub = Hub()
        source = FakeSource(
            FakePubSub([ConnectionError("connection lost")]),
            FakePubSub([]),
        )
        task = asyncio.create_task(hub.run_redis(source, reconnect_delays=(0.01,)))
        for _ in range(200):
            if len(source.created) >= 2 and hub.connected:
                break
            await asyncio.sleep(0.01)

        fresh = hub.subscribe(["ch:x"])
        hub.route("ch:x", change(1))
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        message = await fresh.get(timeout=0.5)
        assert message is not None
        assert message[1].data == {"n": 1}

    async def test_it_is_not_connected_while_redis_is_down(self) -> None:
        hub = Hub()
        source = FakeSource(*[FakePubSub([], fail_to_subscribe=True) for _ in range(50)])
        task = asyncio.create_task(hub.run_redis(source, reconnect_delays=(0.01,)))

        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert not hub.connected
        assert len(source.created) > 2  # it kept trying
