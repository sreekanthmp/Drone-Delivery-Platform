"""Put simulated drones on MQTT: receive commands, answer them, fly, report events.

Events are queued per drone and sent in order; if the broker is unreachable they stay
queued and are retried on the next tick (a real drone retransmits the same way), so the
platform never sees a gap just because the connection blinked.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from uuid import UUID

import structlog

from app.mqtt.client import MqttClient
from app.protocol import messages as wire
from app.protocol import topics
from app.simulator.drone import SimulatedDrone

logger = structlog.get_logger(__name__)


class SimulatorRunner:
    def __init__(
        self, drones: Sequence[SimulatedDrone], client: MqttClient, *, tick_s: float = 0.05
    ) -> None:
        self._drones = {drone.drone_id: drone for drone in drones}
        self._client = client
        self._tick_s = tick_s
        self._outbox: dict[UUID, deque[bytes]] = {drone_id: deque() for drone_id in self._drones}

    async def run(self) -> None:
        """Serve until cancelled."""
        subscriptions = {
            topics.command_topic(drone_id): self._on_command for drone_id in self._drones
        }
        ticker = asyncio.create_task(self._tick_forever())
        try:
            await self._client.run(subscriptions)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    def report(self, drone: SimulatedDrone, events: Sequence[wire.DroneEvent]) -> None:
        """Queue events produced outside the tick (a fault, a manual dock)."""
        for event in events:
            self._outbox[drone.drone_id].append(wire.encode(event))

    # --- commands ----------------------------------------------------------------------------

    async def _on_command(self, topic: str, payload: bytes) -> None:
        drone = self._drones.get(topics.parse_command_topic(topic) or UUID(int=0))
        if drone is None:
            logger.warning("command_for_unknown_drone", topic=topic)
            return
        try:
            command = wire.decode_command(payload)
        except wire.ProtocolError as error:
            logger.warning("command_invalid", drone_id=str(drone.drone_id), reason=str(error))
            return
        ack = drone.on_command(command)
        logger.info(
            "command_handled",
            drone_id=str(drone.drone_id),
            command=command.type,
            ack=None if ack is None else ack.status.value,
            reason=None if ack is None else ack.reason,
        )
        if ack is None:
            return  # deliberately silent (a test of the platform's timeout handling)
        if not topics.is_ack_topic(command.reply_to):
            # A drone only ever answers on the platform's reply topics, whatever it is told.
            logger.warning("reply_topic_refused", reply_to=command.reply_to)
            return
        await self._client.publish(command.reply_to, wire.encode(ack), qos=1)

    # --- time --------------------------------------------------------------------------------

    async def _tick_forever(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self._tick_s)
            now = loop.time()
            elapsed, last = now - last, now
            for drone in self._drones.values():
                self.report(drone, drone.advance(elapsed))
                await self._flush(drone.drone_id)

    async def _flush(self, drone_id: UUID) -> None:
        queue = self._outbox[drone_id]
        while queue:
            try:
                await self._client.publish(topics.event_topic(drone_id), queue[0], qos=1)
            except Exception as error:
                logger.warning("event_publish_deferred", drone_id=str(drone_id), error=repr(error))
                return
            queue.popleft()
