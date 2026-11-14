"""A small, reconnecting MQTT client.

``run`` connects, subscribes, dispatches each message to the handler whose topic filter
matches, and reconnects with back-off when the connection drops. A handler that raises is
logged and skipped: one bad message must never stop the stream.

``publish`` is honest about what it knows. If there is no connection it raises
``ConnectionError``: the message was **definitely not sent**. If the publish itself fails
part-way it raises ``MqttPublishError``: **unknown** whether the broker has it. The drone
link maps those to "rejected" and "no acknowledgement" respectively.

Pass ``durable=True`` (with a stable ``client_id``) for a persistent session, so the broker
holds QoS 1 messages for a subscriber that is briefly away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiomqtt
import structlog

from app.mqtt.url import parse_mqtt_url

logger = structlog.get_logger(__name__)

Handler = Callable[[str, bytes], Awaitable[None]]
"""``handler(topic, payload)``."""

DEFAULT_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0)


class MqttPublishError(Exception):
    """A publish failed part-way: it is unknown whether the broker received the message."""


class MqttClient:
    def __init__(
        self,
        url: str,
        *,
        client_id: str,
        durable: bool = False,
        keepalive: int = 30,
        reconnect_delays: tuple[float, ...] = DEFAULT_RECONNECT_DELAYS,
        client_factory: Callable[..., Any] = aiomqtt.Client,
    ) -> None:
        self._host, self._port = parse_mqtt_url(url)
        self._client_id = client_id
        self._durable = durable
        self._keepalive = keepalive
        self._delays = reconnect_delays or (1.0,)
        self._factory = client_factory
        self._client: Any = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def publish(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        client = self._client
        if client is None:
            raise ConnectionError("not connected to the MQTT broker")
        try:
            await client.publish(topic, payload, qos=qos)
        except aiomqtt.MqttError as error:
            raise MqttPublishError(str(error)) from error

    async def run(self, subscriptions: Mapping[str, Handler]) -> None:
        """Serve forever (until cancelled), reconnecting as needed."""
        attempt = 0
        while True:
            try:
                async with self._factory(
                    self._host,
                    self._port,
                    identifier=self._client_id,
                    clean_session=not self._durable,
                    keepalive=self._keepalive,
                ) as client:
                    for topic_filter in subscriptions:
                        await client.subscribe(topic_filter, qos=1)
                    self._client = client
                    self._connected.set()
                    attempt = 0
                    logger.info("mqtt_connected", client_id=self._client_id)
                    async for message in client.messages:
                        await self._dispatch(subscriptions, message)
            except aiomqtt.MqttError as error:
                logger.warning("mqtt_disconnected", client_id=self._client_id, error=str(error))
            finally:
                self._client = None
                self._connected.clear()
            await asyncio.sleep(self._delays[min(attempt, len(self._delays) - 1)])
            attempt += 1

    @staticmethod
    async def _dispatch(subscriptions: Mapping[str, Handler], message: Any) -> None:
        topic = str(message.topic)
        payload = message.payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif not isinstance(payload, bytes | bytearray):
            payload = b""
        for topic_filter, handler in subscriptions.items():
            if not message.topic.matches(topic_filter):
                continue
            try:
                await handler(topic, bytes(payload))
            except Exception:
                logger.exception("mqtt_handler_failed", topic=topic)
