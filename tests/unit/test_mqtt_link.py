"""MqttDroneLink: five operations, command/ack correlation, and honest failure kinds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.dispatch.link import CommandRejectedError, MissionAck, NoAcknowledgementError
from app.dispatch.mqtt_link import MqttDroneLink
from app.domain.flight_state_machine import FlightStateMachine, StartAuthorization
from app.domain.models import EventSource, FlightStatus, PermissionStatus, PermissionValidation
from app.protocol import messages as wire
from app.protocol import topics
from tests.conftest import DRONE_ID, T0, FakeClock, SequentialIds, build_flight, build_mission

OTHER_DRONE = UUID(int=0xD1)
Responder = Callable[[str, bytes], Awaitable[None]]


class FakeMqtt:
    """A publisher that records messages and lets a test script the drone's reply."""

    def __init__(self) -> None:
        self.published: list[tuple[str, wire.AnyCommand, int]] = []
        self.responder: Responder | None = None
        self.error: Exception | None = None

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        if self.error is not None:
            raise self.error
        self.published.append((topic, wire.decode_command(payload), qos))
        if self.responder is not None:
            responder = self.responder
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(responder(topic, payload))
            )

    def types(self) -> list[str]:
        return [command.type for _, command, _ in self.published]


def ack_for(
    command: wire.AnyCommand, *, status: wire.AckStatus = wire.AckStatus.ACCEPTED, **extra: Any
) -> bytes:
    if isinstance(command, wire.UploadMission) and "mission_checksum" not in extra:
        extra["mission_checksum"] = command.mission.to_domain().checksum
    return wire.encode(
        wire.AckMessage(
            command_id=command.command_id,
            drone_id=command.drone_id,
            status=status,
            acked_at=T0,
            **extra,
        )
    )


@pytest.fixture
def mqtt() -> FakeMqtt:
    return FakeMqtt()


@pytest.fixture
def link(mqtt: FakeMqtt, clock: FakeClock) -> MqttDroneLink:
    return MqttDroneLink(
        mqtt, instance_id="api-1", ack_timeout_s=0.2, clock=clock, id_factory=SequentialIds()
    )


def answer(link: MqttDroneLink, **extra: Any) -> Responder:
    """A drone that accepts every command."""

    async def respond(topic: str, payload: bytes) -> None:
        command = wire.decode_command(payload)
        await link.handle_ack(command.reply_to, ack_for(command, **extra))

    return respond


def authorization(clock: FakeClock, **overrides: Any) -> StartAuthorization:
    flight = build_flight(FlightStatus.MISSION_UPLOADED)
    assert flight.permission is not None
    result = FlightStateMachine(clock=clock).transition(
        flight,
        FlightStatus.STARTED,
        source=EventSource.DISPATCH,
        validation=PermissionValidation(
            flight.permission.permission_id,
            PermissionStatus.VALID,
            clock(),
            flight.permission.valid_until,
        ),
    )
    assert result.start_authorization is not None
    token = result.start_authorization
    if not overrides:
        return token
    return StartAuthorization(
        **{
            "flight_id": token.flight_id,
            "drone_id": token.drone_id,
            "permission_id": token.permission_id,
            "mission_checksum": token.mission_checksum,
            "authorized_at": token.authorized_at,
            "valid_until": token.valid_until,
            **overrides,
        }
    )


class TestWhatIsSent:
    async def test_each_operation_publishes_one_command_to_the_drones_topic_at_qos_1(
        self, link: MqttDroneLink, mqtt: FakeMqtt, clock: FakeClock
    ) -> None:
        mqtt.responder = answer(link)

        await link.upload_mission(DRONE_ID, build_mission())
        await link.start_mission(DRONE_ID, authorization(clock))
        await link.pause_mission(DRONE_ID)
        await link.return_to_home(DRONE_ID)
        await link.land(DRONE_ID)

        assert mqtt.types() == [
            "upload_mission",
            "start_mission",
            "pause_mission",
            "return_to_home",
            "land",
        ]
        assert {topic for topic, _, _ in mqtt.published} == {topics.command_topic(DRONE_ID)}
        assert {qos for _, _, qos in mqtt.published} == {1}

    async def test_only_the_five_command_types_are_ever_published(
        self, link: MqttDroneLink, mqtt: FakeMqtt, clock: FakeClock
    ) -> None:
        mqtt.responder = answer(link)
        await link.upload_mission(DRONE_ID, build_mission())
        await link.start_mission(DRONE_ID, authorization(clock))
        await link.pause_mission(DRONE_ID)
        await link.return_to_home(DRONE_ID)
        await link.land(DRONE_ID)

        assert set(mqtt.types()) == wire.COMMAND_TYPES

    async def test_every_command_names_the_reply_topic_and_has_its_own_id(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link)

        await link.pause_mission(DRONE_ID)
        await link.pause_mission(DRONE_ID)

        commands = [command for _, command, _ in mqtt.published]
        assert {c.reply_to for c in commands} == {"platform/acks/api-1"} == {link.reply_topic}
        assert len({c.command_id for c in commands}) == 2
        assert {c.drone_id for c in commands} == {DRONE_ID}

    async def test_the_mission_is_sent_whole_with_its_checksum(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link)
        mission = build_mission()

        await link.upload_mission(DRONE_ID, mission)

        (_, command, _) = mqtt.published[0]
        assert isinstance(command, wire.UploadMission)
        assert command.mission.to_domain() == mission
        assert command.mission.checksum == mission.checksum

    async def test_a_start_carries_the_authorisation(
        self, link: MqttDroneLink, mqtt: FakeMqtt, clock: FakeClock
    ) -> None:
        mqtt.responder = answer(link)
        token = authorization(clock)

        await link.start_mission(DRONE_ID, token)

        (_, command, _) = mqtt.published[0]
        assert isinstance(command, wire.StartMission)
        assert command.authorization == wire.WireAuthorization.from_domain(token)


class TestAcknowledgements:
    async def test_an_upload_returns_what_the_drone_says_it_holds(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link)
        mission = build_mission()

        ack = await link.upload_mission(DRONE_ID, mission)

        assert ack == MissionAck(DRONE_ID, mission.mission_id, mission.checksum)

    async def test_a_drone_that_echoes_a_different_checksum_is_reported_as_such(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link, mission_checksum="0" * 64)

        ack = await link.upload_mission(DRONE_ID, build_mission())

        assert ack.checksum == "0" * 64  # the service compares it and fails the upload

    async def test_an_upload_ack_without_a_checksum_cannot_match(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        async def silent_about_it(topic: str, payload: bytes) -> None:
            command = wire.decode_command(payload)
            await link.handle_ack(command.reply_to, ack_for(command, mission_checksum=None))

        mqtt.responder = silent_about_it

        ack = await link.upload_mission(DRONE_ID, build_mission())

        assert ack.checksum == ""

    async def test_a_rejection_carries_the_drones_reason(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link, status=wire.AckStatus.REJECTED, reason="motors not armed")

        with pytest.raises(CommandRejectedError, match="motors not armed"):
            await link.pause_mission(DRONE_ID)

    async def test_a_rejection_without_a_reason_still_says_so(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link, status=wire.AckStatus.REJECTED)

        with pytest.raises(CommandRejectedError, match="rejected"):
            await link.land(DRONE_ID)

    async def test_no_answer_in_time_is_an_unknown_not_a_refusal(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        with pytest.raises(NoAcknowledgementError, match="no acknowledgement"):
            await link.return_to_home(DRONE_ID)

        assert mqtt.types() == ["return_to_home"]  # it WAS sent

    async def test_acks_resolve_the_right_command_even_when_they_arrive_out_of_order(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        held: list[wire.AnyCommand] = []

        async def collect(topic: str, payload: bytes) -> None:
            held.append(wire.decode_command(payload))

        mqtt.responder = collect
        first = asyncio.create_task(link.pause_mission(DRONE_ID))
        second = asyncio.create_task(link.upload_mission(OTHER_DRONE, build_mission()))
        while len(held) < 2:
            await asyncio.sleep(0)

        for command in reversed(held):  # the second command is answered first
            await link.handle_ack(command.reply_to, ack_for(command))

        assert await first is None
        assert (await second).drone_id == OTHER_DRONE


class TestFailureKinds:
    async def test_not_connected_means_it_was_definitely_not_sent(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.error = ConnectionError("not connected to the MQTT broker")

        with pytest.raises(CommandRejectedError, match="not sent"):
            await link.pause_mission(DRONE_ID)

    async def test_a_publish_that_fails_part_way_is_unknown(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.error = RuntimeError("socket died while publishing")

        with pytest.raises(NoAcknowledgementError, match="part-way"):
            await link.pause_mission(DRONE_ID)

    async def test_nothing_is_left_pending_after_any_outcome(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        mqtt.responder = answer(link)
        await link.pause_mission(DRONE_ID)  # success
        mqtt.responder = None
        with pytest.raises(NoAcknowledgementError):
            await link.pause_mission(DRONE_ID)  # timeout
        mqtt.error = ConnectionError("down")
        with pytest.raises(CommandRejectedError):
            await link.pause_mission(DRONE_ID)  # not sent
        mqtt.error = RuntimeError("x")
        with pytest.raises(NoAcknowledgementError):
            await link.pause_mission(DRONE_ID)  # unknown

        assert link._pending == {}

    async def test_a_cancelled_command_leaves_nothing_pending(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        task = asyncio.create_task(link.land(DRONE_ID))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert link._pending == {}


class TestAcksThatMatchNothing:
    async def test_a_late_ack_after_the_timeout_is_ignored(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        with pytest.raises(NoAcknowledgementError):
            await link.land(DRONE_ID)
        (_, command, _) = mqtt.published[0]

        await link.handle_ack(command.reply_to, ack_for(command))  # must not raise

        assert link._pending == {}

    async def test_a_duplicate_ack_is_harmless(self, link: MqttDroneLink, mqtt: FakeMqtt) -> None:
        async def twice(topic: str, payload: bytes) -> None:
            command = wire.decode_command(payload)
            await link.handle_ack(command.reply_to, ack_for(command))
            await link.handle_ack(command.reply_to, ack_for(command))

        mqtt.responder = twice

        await link.pause_mission(DRONE_ID)

    async def test_an_ack_for_an_unknown_command_is_ignored(self, link: MqttDroneLink) -> None:
        ack = wire.encode(
            wire.AckMessage(
                command_id=UUID(int=999),
                drone_id=DRONE_ID,
                status=wire.AckStatus.ACCEPTED,
                acked_at=T0,
            )
        )

        await link.handle_ack(link.reply_topic, ack)

    async def test_an_ack_from_a_different_drone_does_not_resolve_the_command(
        self, link: MqttDroneLink, mqtt: FakeMqtt
    ) -> None:
        async def imposter(topic: str, payload: bytes) -> None:
            command = wire.decode_command(payload)
            forged = wire.AckMessage(
                command_id=command.command_id,
                drone_id=OTHER_DRONE,
                status=wire.AckStatus.ACCEPTED,
                acked_at=T0,
            )
            await link.handle_ack(command.reply_to, wire.encode(forged))

        mqtt.responder = imposter

        with pytest.raises(NoAcknowledgementError):
            await link.pause_mission(DRONE_ID)

    @pytest.mark.parametrize("payload", [b"", b"garbage", b"{}", b'{"command_id": 1}'])
    async def test_a_malformed_ack_is_ignored(self, link: MqttDroneLink, payload: bytes) -> None:
        await link.handle_ack(link.reply_topic, payload)


class TestAStartIsNeverSentWithoutAValidAuthorisation:
    async def test_not_an_authorisation(self, link: MqttDroneLink, mqtt: FakeMqtt) -> None:
        with pytest.raises(CommandRejectedError, match="StartAuthorization"):
            await link.start_mission(DRONE_ID, object())  # type: ignore[arg-type]

        assert mqtt.published == []

    async def test_for_another_drone(
        self, link: MqttDroneLink, mqtt: FakeMqtt, clock: FakeClock
    ) -> None:
        with pytest.raises(CommandRejectedError, match="different drone"):
            await link.start_mission(OTHER_DRONE, authorization(clock))

        assert mqtt.published == []

    async def test_expired(self, link: MqttDroneLink, mqtt: FakeMqtt, clock: FakeClock) -> None:
        token = authorization(clock)
        clock.now = token.valid_until + timedelta(seconds=1)

        with pytest.raises(CommandRejectedError, match="expired"):
            await link.start_mission(DRONE_ID, token)

        assert mqtt.published == []


class TestConstruction:
    def test_the_timeout_must_be_positive(self, mqtt: FakeMqtt) -> None:
        with pytest.raises(ValueError, match="ack_timeout_s"):
            MqttDroneLink(mqtt, instance_id="a", ack_timeout_s=0)

    @pytest.mark.parametrize("instance_id", ["", "a/b", "a+b", "#"])
    def test_the_instance_id_cannot_be_a_wildcard_or_path(
        self, mqtt: FakeMqtt, instance_id: str
    ) -> None:
        with pytest.raises(ValueError, match="invalid instance id"):
            MqttDroneLink(mqtt, instance_id=instance_id)
