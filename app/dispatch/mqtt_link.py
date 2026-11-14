"""The drone link over MQTT (Rule 1: five operations, nothing finer).

Each operation publishes one command to ``drones/{id}/commands`` and waits for the drone's
acknowledgement on this process's reply topic, matched by ``command_id``:

* no connection to the broker: the command was **definitely not sent**, so it is
  ``CommandRejectedError``;
* published but no acknowledgement in time (or the publish failed part-way): **unknown**,
  so ``NoAcknowledgementError``. The service recovers from these two differently;
* the drone answers "rejected": ``CommandRejectedError`` with its reason.

A start carries the Rule 2 authorisation so the drone can refuse an expired or mismatched
one itself, and the link refuses to send one that is already expired or for another drone.
Acknowledgements that match nothing (late, duplicated, for another drone) are ignored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, TypedDict
from uuid import UUID, uuid4

import structlog

from app.dispatch.link import CommandRejectedError, MissionAck, NoAcknowledgementError
from app.domain.flight_state_machine import StartAuthorization
from app.domain.models import Mission
from app.protocol import messages as wire
from app.protocol import topics

logger = structlog.get_logger(__name__)


class _Envelope(TypedDict):
    command_id: UUID
    drone_id: UUID
    sent_at: datetime
    reply_to: str


class MqttPublisher(Protocol):
    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        """Raises ``ConnectionError`` if the message was definitely not sent; any other
        exception means it is unknown whether the broker received it."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MqttDroneLink:
    def __init__(
        self,
        mqtt: MqttPublisher,
        *,
        instance_id: str,
        ack_timeout_s: float = 10.0,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if ack_timeout_s <= 0:
            raise ValueError("ack_timeout_s must be positive")
        self._mqtt = mqtt
        self._reply_to = topics.ack_topic(instance_id)
        self._timeout = ack_timeout_s
        self._clock = clock
        self._ids = id_factory
        self._pending: dict[UUID, tuple[UUID, asyncio.Future[wire.AckMessage]]] = {}

    @property
    def reply_topic(self) -> str:
        """Subscribe to this to receive the acknowledgements of this process's commands."""
        return self._reply_to

    # --- DroneLink ---------------------------------------------------------------------------

    async def upload_mission(self, drone_id: UUID, mission: Mission) -> MissionAck:
        command = wire.UploadMission(
            **self._envelope(drone_id), mission=wire.WireMission.from_domain(mission)
        )
        ack = await self._send(command)
        return MissionAck(
            drone_id=drone_id, mission_id=mission.mission_id, checksum=ack.mission_checksum or ""
        )

    async def start_mission(self, drone_id: UUID, authorization: StartAuthorization) -> None:
        if not isinstance(authorization, StartAuthorization):
            raise CommandRejectedError("a start needs a StartAuthorization")
        if authorization.drone_id != drone_id:
            raise CommandRejectedError("the authorisation is for a different drone")
        if self._clock() >= authorization.valid_until:
            raise CommandRejectedError("the authorisation has expired")
        await self._send(
            wire.StartMission(
                **self._envelope(drone_id),
                authorization=wire.WireAuthorization.from_domain(authorization),
            )
        )

    async def pause_mission(self, drone_id: UUID) -> None:
        await self._send(wire.PauseMission(**self._envelope(drone_id)))

    async def return_to_home(self, drone_id: UUID) -> None:
        await self._send(wire.ReturnToHome(**self._envelope(drone_id)))

    async def land(self, drone_id: UUID) -> None:
        await self._send(wire.Land(**self._envelope(drone_id)))

    # --- acknowledgements --------------------------------------------------------------------

    async def handle_ack(self, topic: str, payload: bytes) -> None:
        """Resolve the command an acknowledgement answers. Anything unmatched is ignored."""
        try:
            ack = wire.decode_ack(payload)
        except wire.ProtocolError as error:
            logger.warning("ack_invalid", topic=topic, reason=str(error))
            return
        pending = self._pending.get(ack.command_id)
        if pending is None:
            logger.info("ack_unmatched", command_id=str(ack.command_id))  # late or repeated
            return
        drone_id, future = pending
        if ack.drone_id != drone_id:
            logger.warning("ack_from_wrong_drone", command_id=str(ack.command_id))
            return
        if not future.done():
            future.set_result(ack)

    # --- internals ---------------------------------------------------------------------------

    def _envelope(self, drone_id: UUID) -> _Envelope:
        return {
            "command_id": self._ids(),
            "drone_id": drone_id,
            "sent_at": self._clock(),
            "reply_to": self._reply_to,
        }

    async def _send(self, command: wire.AnyCommand) -> wire.AckMessage:
        future: asyncio.Future[wire.AckMessage] = asyncio.get_running_loop().create_future()
        self._pending[command.command_id] = (command.drone_id, future)
        try:
            try:
                await self._mqtt.publish(
                    topics.command_topic(command.drone_id), wire.encode(command), qos=1
                )
            except ConnectionError as error:
                raise CommandRejectedError(f"not sent: {error}") from error
            except Exception as error:
                raise NoAcknowledgementError(f"publish failed part-way: {error}") from error
            try:
                ack = await asyncio.wait_for(future, self._timeout)
            except TimeoutError:
                raise NoAcknowledgementError(
                    f"no acknowledgement within {self._timeout:g}s"
                ) from None
        finally:
            self._pending.pop(command.command_id, None)
        if ack.status is wire.AckStatus.REJECTED:
            raise CommandRejectedError(ack.reason or "the drone rejected the command")
        return ack
