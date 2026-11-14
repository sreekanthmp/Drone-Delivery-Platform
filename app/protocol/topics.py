"""MQTT topic names. Every topic is built here, so a typo cannot create a new channel.

=====================================  ==========  ========================================
Topic                                  Direction   Carries
=====================================  ==========  ========================================
``drones/{drone_id}/commands``         cloud->drone  one of the five high-level commands
``platform/acks/{instance_id}``        drone->cloud  the drone's answer to a command
``drones/{drone_id}/events``           drone->cloud  mission progress events
=====================================  ==========  ========================================

Acks go to a per-process topic named in the command (``reply_to``), so any number of API
processes can send commands without receiving each other's answers.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

EVENTS_FILTER: Final = "drones/+/events"
"""Subscribe to this to receive the events of every drone."""

_INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_EVENT_TOPIC = re.compile(r"drones/(?P<drone_id>[0-9a-fA-F-]{36})/events")
_COMMAND_TOPIC = re.compile(r"drones/(?P<drone_id>[0-9a-fA-F-]{36})/commands")


def command_topic(drone_id: UUID) -> str:
    return f"drones/{drone_id}/commands"


def event_topic(drone_id: UUID) -> str:
    return f"drones/{drone_id}/events"


def ack_topic(instance_id: str) -> str:
    """The reply topic of one platform process. ``instance_id`` may not contain wildcards."""
    if not _INSTANCE_ID.fullmatch(instance_id):
        raise ValueError(f"invalid instance id {instance_id!r}")
    return f"platform/acks/{instance_id}"


def is_ack_topic(topic: str) -> bool:
    prefix = "platform/acks/"
    return topic.startswith(prefix) and bool(_INSTANCE_ID.fullmatch(topic[len(prefix) :]))


def parse_event_topic(topic: str) -> UUID | None:
    """The drone an event topic belongs to, or ``None`` if it is not a valid event topic."""
    match = _EVENT_TOPIC.fullmatch(topic)
    if match is None:
        return None
    try:
        return UUID(match["drone_id"])
    except ValueError:
        return None


def parse_command_topic(topic: str) -> UUID | None:
    """The drone a command topic belongs to (used by whoever plays the drone)."""
    match = _COMMAND_TOPIC.fullmatch(topic)
    if match is None:
        return None
    try:
        return UUID(match["drone_id"])
    except ValueError:
        return None
