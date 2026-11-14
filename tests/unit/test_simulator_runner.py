"""The simulator on MQTT: command routing, acknowledgements, and ordered event delivery."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest

from app.protocol import messages as wire
from app.protocol import topics
from app.simulator.drone import SimulatedDrone
from app.simulator.runner import SimulatorRunner
from tests.conftest import DRONE_ID, HOME, FakeClock, build_mission
from tests.unit.test_simulator import FULL_FLIGHT, envelope, start, upload

OTHER = UUID(int=0xD1)


class FakeClient:
    """Stands in for ``MqttClient``: records publishes, can be made to fail, never connects."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, Any] = {}
        self.published: list[tuple[str, bytes, int]] = []
        self.fail = False

    async def run(self, subscriptions: dict[str, Any]) -> None:
        self.subscriptions = dict(subscriptions)
        await asyncio.Event().wait()

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append((topic, payload, qos))

    def events(self) -> list[wire.DroneEvent]:
        return [
            wire.decode_event(payload)
            for topic, payload, _ in self.published
            if topic == topics.event_topic(DRONE_ID)
        ]

    def non_telemetry(self) -> list[tuple[str, bytes, int]]:
        """Everything published except telemetry samples (which tick on their own schedule)."""
        return [m for m in self.published if "telemetry" not in m[0]]

    def samples(self) -> list[wire.Telemetry]:
        return [
            wire.decode_telemetry(payload)
            for topic, payload, _ in self.published
            if topic == topics.telemetry_topic(DRONE_ID)
        ]

    def acks(self) -> list[tuple[str, wire.AckMessage]]:
        return [
            (topic, wire.decode_ack(payload))
            for topic, payload, _ in self.published
            if topics.is_ack_topic(topic)
        ]


async def until(condition: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def drone(clock: FakeClock) -> SimulatedDrone:
    return SimulatedDrone(DRONE_ID, home=HOME, speed_factor=5000.0, hold_s=2.0, clock=clock)


@pytest.fixture
async def runner(client: FakeClient, drone: SimulatedDrone) -> AsyncIterator[SimulatorRunner]:
    runner = SimulatorRunner([drone], client, tick_s=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(runner.run())
    await until(lambda: bool(client.subscriptions))
    yield runner
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def encode(command: wire.AnyCommand) -> bytes:
    return wire.encode(command)


class TestCommands:
    async def test_it_listens_on_each_drones_command_topic(
        self, runner: SimulatorRunner, client: FakeClient
    ) -> None:
        assert set(client.subscriptions) == {topics.command_topic(DRONE_ID)}

    async def test_a_command_is_acknowledged_on_the_reply_topic_at_qos_1(
        self, runner: SimulatorRunner, client: FakeClient
    ) -> None:
        command = upload()

        await runner._on_command(topics.command_topic(DRONE_ID), encode(command))

        ((topic, ack),) = client.acks()
        assert topic == "platform/acks/test"
        assert ack.command_id == command.command_id
        assert ack.status is wire.AckStatus.ACCEPTED
        assert ack.mission_checksum == build_mission().checksum
        assert {qos for _, _, qos in client.published} == {1}

    async def test_it_never_answers_on_a_topic_that_is_not_a_platform_reply_topic(
        self, runner: SimulatorRunner, client: FakeClient
    ) -> None:
        command = wire.PauseMission(**{**envelope(), "reply_to": "drones/someone/events"})

        await runner._on_command(topics.command_topic(DRONE_ID), encode(command))

        assert client.non_telemetry() == []

    async def test_a_silent_drone_does_not_acknowledge(
        self, runner: SimulatorRunner, client: FakeClient, drone: SimulatedDrone
    ) -> None:
        drone.silent.add("upload_mission")

        await runner._on_command(topics.command_topic(DRONE_ID), encode(upload()))

        assert client.acks() == []
        assert drone.mission is not None  # but it did take the mission

    @pytest.mark.parametrize(
        ("topic", "payload"),
        [
            (topics.command_topic(OTHER), b"{}"),  # not one of ours
            ("drones/garbage/commands", b"{}"),
            (topics.command_topic(DRONE_ID), b"not json"),
            (topics.command_topic(DRONE_ID), b'{"type": "goto"}'),
        ],
    )
    async def test_junk_is_ignored(
        self, runner: SimulatorRunner, client: FakeClient, topic: str, payload: bytes
    ) -> None:
        await runner._on_command(topic, payload)

        assert client.non_telemetry() == []


class TestEvents:
    async def test_a_flight_is_reported_in_order_on_the_events_topic(
        self, runner: SimulatorRunner, client: FakeClient, clock: FakeClock
    ) -> None:
        await runner._on_command(topics.command_topic(DRONE_ID), encode(upload()))
        await runner._on_command(topics.command_topic(DRONE_ID), encode(start(clock)))

        await until(lambda: len(client.events()) == 5)

        events = client.events()
        assert [e.type for e in events] == FULL_FLIGHT
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]
        assert {qos for topic, _, qos in client.published if "events" in topic} == {1}

    async def test_events_survive_a_broker_outage_in_order_without_loss_or_repeats(
        self, runner: SimulatorRunner, client: FakeClient, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        await runner._on_command(topics.command_topic(DRONE_ID), encode(upload()))
        client.fail = True  # the broker goes away just as the flight starts
        with pytest.raises(ConnectionError):  # the drone acts, but even its ack cannot be sent
            await runner._on_command(topics.command_topic(DRONE_ID), encode(start(clock)))

        await until(lambda: drone.phase.value == "idle")  # the whole flight happens meanwhile
        assert client.events() == []  # nothing got through

        client.fail = False
        await until(lambda: len(client.events()) == 5)

        assert [e.seq for e in client.events()] == [1, 2, 3, 4, 5]
        assert [e.type for e in client.events()] == FULL_FLIGHT

    async def test_a_reported_fault_is_published_on_the_next_tick(
        self, runner: SimulatorRunner, client: FakeClient, drone: SimulatedDrone, clock: FakeClock
    ) -> None:
        await runner._on_command(topics.command_topic(DRONE_ID), encode(upload()))
        await runner._on_command(topics.command_topic(DRONE_ID), encode(start(clock)))
        runner.report(drone, drone.fault("GPS_LOSS"))

        await until(lambda: any(e.type is wire.EventType.FAULT for e in client.events()))


class TestTelemetry:
    @pytest.fixture
    async def sampling(
        self, client: FakeClient, drone: SimulatedDrone
    ) -> AsyncIterator[SimulatorRunner]:
        runner = SimulatorRunner([drone], client, tick_s=0.01, telemetry_interval_s=0.03)  # type: ignore[arg-type]
        task = asyncio.create_task(runner.run())
        await until(lambda: bool(client.subscriptions))
        yield runner
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_samples_are_published_regularly_at_qos_0_even_when_idle(
        self, sampling: SimulatorRunner, client: FakeClient
    ) -> None:
        await until(lambda: len(client.samples()) >= 4)

        assert {s.phase for s in client.samples()} == {wire.DronePhase.IDLE}
        sample_publishes = [
            (topic, qos) for topic, _, qos in client.published if "telemetry" in topic
        ]
        assert {qos for _, qos in sample_publishes} == {0}
        assert {topic for topic, _ in sample_publishes} == {topics.telemetry_topic(DRONE_ID)}

    async def test_samples_follow_the_flight(
        self, sampling: SimulatorRunner, client: FakeClient, clock: FakeClock
    ) -> None:
        await sampling._on_command(topics.command_topic(DRONE_ID), encode(upload()))
        await sampling._on_command(topics.command_topic(DRONE_ID), encode(start(clock)))

        await until(lambda: any(s.phase is wire.DronePhase.FLYING for s in client.samples()))

        flying = next(s for s in client.samples() if s.phase is wire.DronePhase.FLYING)
        assert flying.flight_id is not None
        assert flying.altitude_m > 0

    async def test_a_muted_drone_goes_silent_but_still_flies_and_reports_events(
        self, sampling: SimulatorRunner, client: FakeClient, clock: FakeClock, drone: SimulatedDrone
    ) -> None:
        await until(lambda: len(client.samples()) >= 2)
        sampling.muted.add(DRONE_ID)
        await asyncio.sleep(0.05)
        seen = len(client.samples())

        await sampling._on_command(topics.command_topic(DRONE_ID), encode(upload()))
        await sampling._on_command(topics.command_topic(DRONE_ID), encode(start(clock)))
        await until(lambda: len(client.events()) == 5)

        assert len(client.samples()) == seen  # not one more sample
        assert drone.phase.value == "idle"  # and the mission completed meanwhile

    async def test_a_publish_failure_just_drops_the_sample(
        self, sampling: SimulatorRunner, client: FakeClient
    ) -> None:
        client.fail = True
        await asyncio.sleep(0.1)
        client.fail = False

        await until(lambda: len(client.samples()) >= 1)  # it kept going
