"""The reconnecting MQTT client, against a fake aiomqtt."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import aiomqtt
import pytest

from app.mqtt.client import MqttClient, MqttPublishError, parse_mqtt_url


class FakeMessage:
    def __init__(self, topic: str, payload: Any) -> None:
        self.topic = aiomqtt.Topic(topic)
        self.payload = payload


class Script:
    """What one connection attempt does: refuse, or deliver messages then (optionally) drop."""

    def __init__(
        self,
        *,
        refuse: bool = False,
        messages: tuple[FakeMessage, ...] = (),
        then: str = "hold",  # 'hold' the connection open, or 'drop' it
    ) -> None:
        self.refuse, self.messages, self.then = refuse, messages, then


class FakeAio:
    """A stand-in for ``aiomqtt.Client`` driven by a list of scripts, one per connection."""

    scripts: ClassVar[list[Script]] = []
    created: ClassVar[list[FakeAio]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.subscribed: list[tuple[str, int]] = []
        self.sent: list[tuple[str, bytes, int]] = []
        self.publish_error: Exception | None = None
        self.script = FakeAio.scripts.pop(0) if FakeAio.scripts else Script()
        FakeAio.created.append(self)

    async def __aenter__(self) -> FakeAio:
        if self.script.refuse:
            raise aiomqtt.MqttError("connection refused")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        if self.publish_error is not None:
            raise self.publish_error
        self.sent.append((topic, payload, qos))

    @property
    def messages(self) -> AsyncIterator[FakeMessage]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[FakeMessage]:
        for message in self.script.messages:
            yield message
        if self.script.then == "drop":
            raise aiomqtt.MqttError("connection lost")
        await asyncio.Event().wait()  # hold the connection open until cancelled


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeAio.scripts = []
    FakeAio.created = []


def make(**kwargs: Any) -> MqttClient:
    kwargs.setdefault("client_id", "test")
    kwargs.setdefault("reconnect_delays", (0.0,))
    return MqttClient("mqtt://broker.test:1884", client_factory=FakeAio, **kwargs)


class Recorder:
    def __init__(self, fail_on: bytes | None = None) -> None:
        self.seen: list[tuple[str, bytes]] = []
        self.fail_on = fail_on

    async def __call__(self, topic: str, payload: bytes) -> None:
        if payload == self.fail_on:
            raise RuntimeError("handler bug")
        self.seen.append((topic, payload))


async def until(condition: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


class TestUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("mqtt://localhost", ("localhost", 1883)),
            ("mqtt://mosquitto:1884", ("mosquitto", 1884)),
            ("mqtt://10.0.0.5:9", ("10.0.0.5", 9)),
        ],
    )
    def test_valid(self, url: str, expected: tuple[str, int]) -> None:
        assert parse_mqtt_url(url) == expected

    @pytest.mark.parametrize(
        "url", ["", "localhost", "http://x", "mqtts://x", "mqtt://", "mqtt:///p"]
    )
    def test_invalid(self, url: str) -> None:
        with pytest.raises(ValueError, match="mqtt://"):
            parse_mqtt_url(url)


class TestRunning:
    async def test_it_connects_subscribes_at_qos_1_and_reports_connected(self) -> None:
        client = make(durable=True, client_id="worker-1")
        task = asyncio.create_task(client.run({"a/+": Recorder(), "b/#": Recorder()}))

        await client.wait_connected(timeout=2)

        (fake,) = FakeAio.created
        assert (fake.host, fake.port) == ("broker.test", 1884)
        assert fake.kwargs["identifier"] == "worker-1"
        assert fake.kwargs["clean_session"] is False  # durable
        assert fake.subscribed == [("a/+", 1), ("b/#", 1)]
        assert client.connected
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.connected

    async def test_a_non_durable_client_asks_for_a_clean_session(self) -> None:
        client = make()
        task = asyncio.create_task(client.run({"a": Recorder()}))
        await client.wait_connected(timeout=2)

        assert FakeAio.created[0].kwargs["clean_session"] is True
        task.cancel()

    async def test_messages_go_to_the_handler_whose_filter_matches(self) -> None:
        events, acks = Recorder(), Recorder()
        FakeAio.scripts = [
            Script(
                messages=(
                    FakeMessage("drones/d1/events", b"e1"),
                    FakeMessage("platform/acks/x", b"a1"),
                    FakeMessage("drones/d2/events", "text-payload"),
                    FakeMessage("drones/d3/events", None),
                    FakeMessage("unrelated/topic", b"ignored"),
                )
            )
        ]
        client = make()
        task = asyncio.create_task(client.run({"drones/+/events": events, "platform/acks/+": acks}))

        await until(lambda: len(events.seen) == 3 and len(acks.seen) == 1)

        assert events.seen == [
            ("drones/d1/events", b"e1"),
            ("drones/d2/events", b"text-payload"),
            ("drones/d3/events", b""),
        ]
        assert acks.seen == [("platform/acks/x", b"a1")]
        task.cancel()

    async def test_a_failing_handler_does_not_stop_the_stream(self) -> None:
        handler = Recorder(fail_on=b"poison")
        FakeAio.scripts = [
            Script(
                messages=(
                    FakeMessage("t", b"before"),
                    FakeMessage("t", b"poison"),
                    FakeMessage("t", b"after"),
                )
            )
        ]
        client = make()
        task = asyncio.create_task(client.run({"t": handler}))

        await until(lambda: len(handler.seen) == 2)

        assert [payload for _, payload in handler.seen] == [b"before", b"after"]
        task.cancel()


class TestReconnecting:
    async def test_it_reconnects_after_the_connection_drops_and_resubscribes(self) -> None:
        handler = Recorder()
        FakeAio.scripts = [
            Script(messages=(FakeMessage("t", b"one"),), then="drop"),
            Script(messages=(FakeMessage("t", b"two"),)),
        ]
        client = make()
        task = asyncio.create_task(client.run({"t": handler}))

        await until(lambda: len(handler.seen) == 2)

        assert [payload for _, payload in handler.seen] == [b"one", b"two"]
        assert len(FakeAio.created) == 2
        assert all(c.subscribed == [("t", 1)] for c in FakeAio.created)
        task.cancel()

    async def test_it_keeps_trying_while_the_broker_is_unreachable(self) -> None:
        FakeAio.scripts = [Script(refuse=True), Script(refuse=True), Script(refuse=True)]
        client = make()
        task = asyncio.create_task(client.run({"t": Recorder()}))

        await client.wait_connected(timeout=2)

        assert len(FakeAio.created) == 4  # three refusals, then a connection
        assert client.connected
        task.cancel()

    async def test_it_is_not_connected_between_attempts(self) -> None:
        FakeAio.scripts = [Script(then="drop"), Script(refuse=True)]
        client = make(reconnect_delays=(0.05,))
        task = asyncio.create_task(client.run({"t": Recorder()}))
        await client.wait_connected(timeout=2)

        await until(lambda: not client.connected)

        assert not client.connected
        task.cancel()


class TestPublishing:
    async def test_without_a_connection_it_is_definitely_not_sent(self) -> None:
        client = make()

        with pytest.raises(ConnectionError, match="not connected"):
            await client.publish("t", b"x")

    async def test_it_publishes_through_the_connection(self) -> None:
        client = make()
        task = asyncio.create_task(client.run({"t": Recorder()}))
        await client.wait_connected(timeout=2)

        await client.publish("drones/x/commands", b"payload", qos=1)

        assert FakeAio.created[0].sent == [("drones/x/commands", b"payload", 1)]
        task.cancel()

    async def test_a_failure_part_way_is_a_publish_error_not_a_connection_error(self) -> None:
        client = make()
        task = asyncio.create_task(client.run({"t": Recorder()}))
        await client.wait_connected(timeout=2)
        FakeAio.created[0].publish_error = aiomqtt.MqttError("socket died")

        with pytest.raises(MqttPublishError, match="socket died"):
            await client.publish("t", b"x")

        assert not issubclass(MqttPublishError, ConnectionError)
        task.cancel()
