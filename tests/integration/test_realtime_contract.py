"""The live view's contract, run against the in-memory implementation AND a real Redis.

The same assertions on both keep the fake honest; the Redis-only tests at the end cover what
only a real server can show (key expiry, pub/sub delivery, an unreachable server).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.realtime import channels
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Alert, AlertKind, DroneState, Envelope
from app.realtime.redis_backend import RedisRealtime
from tests.conftest import DRONE_ID, T0
from tests.unit.test_realtime import alert, state

OTHER = UUID(int=0xD1)


@pytest.fixture(
    params=["memory", pytest.param("redis", marks=pytest.mark.integration)],
)
def live(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryRealtime(clock=lambda: T0, recent_max=3)  # samples are stamped T0
    client = request.getfixturevalue("redis_client")
    return RedisRealtime(client, recent_max=3)


class TestState:
    async def test_set_and_get(self, live: Any) -> None:
        await live.set_state(state())

        assert await live.get_state(DRONE_ID) == state()

    async def test_a_drone_never_heard_from_has_no_state(self, live: Any) -> None:
        assert await live.get_state(OTHER) is None

    async def test_the_latest_sample_replaces_the_previous_one(self, live: Any) -> None:
        await live.set_state(state(battery_percent=90.0))
        await live.set_state(state(battery_percent=60.0))

        current = await live.get_state(DRONE_ID)
        assert current is not None
        assert current.battery_percent == 60.0

    async def test_all_states_returns_every_drone(self, live: Any) -> None:
        await live.set_state(state(DRONE_ID))
        await live.set_state(state(OTHER, battery_percent=10.0))

        everything = await live.all_states()

        assert set(everything) == {DRONE_ID, OTHER}
        assert everything[OTHER].battery_percent == 10.0

    async def test_no_states_is_an_empty_mapping(self, live: Any) -> None:
        assert await live.all_states() == {}


class TestAlerts:
    async def test_a_new_alert_is_active_and_a_repeat_is_not_new(self, live: Any) -> None:
        assert await live.raise_alert(alert()) is True
        assert await live.raise_alert(alert()) is False

        assert [a.key for a in await live.active()] == [alert().key]

    async def test_alerts_are_independent_per_kind_and_per_drone(self, live: Any) -> None:
        assert await live.raise_alert(alert(AlertKind.LINK_LOST, DRONE_ID))
        assert await live.raise_alert(alert(AlertKind.LOW_BATTERY, DRONE_ID))
        assert await live.raise_alert(alert(AlertKind.LINK_LOST, OTHER))

        assert len(await live.active()) == 3

    async def test_resolving_clears_it_once(self, live: Any) -> None:
        await live.raise_alert(alert())

        assert await live.resolve(AlertKind.LINK_LOST, DRONE_ID, at=T0) is True
        assert await live.resolve(AlertKind.LINK_LOST, DRONE_ID, at=T0) is False
        assert await live.active() == []

    async def test_resolving_something_never_raised_is_a_no_op(self, live: Any) -> None:
        assert await live.resolve(AlertKind.LOW_BATTERY, OTHER, at=T0) is False

    async def test_it_can_be_raised_again_after_it_was_resolved(self, live: Any) -> None:
        await live.raise_alert(alert())
        await live.resolve(AlertKind.LINK_LOST, DRONE_ID, at=T0)

        assert await live.raise_alert(alert()) is True

    async def test_a_one_off_alert_is_recorded_but_never_active(self, live: Any) -> None:
        await live.notify(alert(AlertKind.FAULT))
        await live.notify(alert(AlertKind.FAULT))

        assert await live.active() == []
        assert len(await live.recent()) == 2

    async def test_the_recent_list_is_newest_first_bounded_and_shows_resolutions(
        self, live: Any
    ) -> None:
        await live.raise_alert(alert(AlertKind.LINK_LOST))
        await live.resolve(AlertKind.LINK_LOST, DRONE_ID, at=T0 + timedelta(seconds=5))
        await live.notify(alert(AlertKind.FAULT))

        recent: list[Alert] = await live.recent()

        assert [(a.kind, a.resolved_at is not None) for a in recent] == [
            (AlertKind.FAULT, False),
            (AlertKind.LINK_LOST, True),
            (AlertKind.LINK_LOST, False),
        ]
        await live.notify(alert(AlertKind.FAULT, OTHER))  # a fourth: the oldest falls off
        assert len(await live.recent()) == 3
        assert len(await live.recent(limit=2)) == 2

    async def test_raising_and_resolving_are_broadcast_to_the_fleet(self, live: Any) -> None:
        sent: list[Envelope] = []
        if isinstance(live, InMemoryRealtime):
            live.listeners.append(lambda channel, envelope: sent.append(envelope))
            await live.raise_alert(alert())
            await live.resolve(AlertKind.LINK_LOST, DRONE_ID, at=T0)
            assert [e.kind for e in sent] == ["alert", "alert"]
            assert [e.data["resolved_at"] is not None for e in sent] == [False, True]
            assert {e.droppable for e in sent} == {False}


@pytest.mark.integration
class TestRedisOnly:
    async def test_a_state_expires_when_the_drone_goes_silent(self, redis_client: Any) -> None:
        live = RedisRealtime(redis_client, state_ttl=timedelta(milliseconds=150))
        await live.set_state(state())
        assert await live.get_state(DRONE_ID) is not None

        await asyncio.sleep(0.3)

        assert await live.get_state(DRONE_ID) is None
        assert await live.all_states() == {}

    async def test_a_published_envelope_reaches_a_subscriber_intact(
        self, redis_client: Any
    ) -> None:
        live = RedisRealtime(redis_client)
        pubsub = redis_client.pubsub()
        await pubsub.psubscribe(channels.PATTERN)
        await pubsub.get_message(timeout=1)  # the subscription confirmation
        envelope = Envelope(kind="drone_state", droppable=True, data={"battery": 71})

        await live.publish(channels.drone_channel(DRONE_ID), envelope)

        message = await pubsub.get_message(timeout=2)
        assert message is not None
        assert message["channel"] == channels.drone_channel(DRONE_ID)
        assert Envelope.decode(message["data"]) == envelope
        await pubsub.aclose()

    async def test_raising_an_alert_publishes_it_on_the_fleet_channel(
        self, redis_client: Any
    ) -> None:
        live = RedisRealtime(redis_client)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channels.FLEET)
        await pubsub.get_message(timeout=1)

        await live.raise_alert(alert())

        message = await pubsub.get_message(timeout=2)
        assert message is not None
        assert Envelope.decode(message["data"]).data["kind"] == "link_lost"
        await pubsub.aclose()

    async def test_state_survives_json_round_tripping_through_redis(
        self, redis_client: Any
    ) -> None:
        live = RedisRealtime(redis_client)
        original = DroneState.model_validate(state().model_dump())

        await live.set_state(original)

        assert await live.get_state(DRONE_ID) == original

    async def test_an_unreachable_redis_fails_reads_but_never_a_broadcast(self) -> None:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
        live = RedisRealtime(client)

        await live.publish(channels.FLEET, Envelope(kind="alert", droppable=False, data={}))
        with pytest.raises(Exception):  # noqa: B017, PT011 - any connection error will do
            await live.get_state(DRONE_ID)
        await client.aclose()
