"""Names of the live channels and keys, built in one place."""

from __future__ import annotations

from typing import Final
from uuid import UUID

FLEET: Final = "ch:fleet"
"""Everything an operator watching the whole fleet sees: samples, changes, alerts."""

PATTERN: Final = "ch:*"
"""Every live channel (one pattern subscription per process; routing is done in-process)."""

STATE_KEY_PREFIX: Final = "state:"
ACTIVE_ALERTS_KEY: Final = "alerts:active"
RECENT_ALERTS_KEY: Final = "alerts:recent"


def drone_channel(drone_id: UUID) -> str:
    return f"ch:drone:{drone_id}"


def order_channel(order_id: UUID) -> str:
    return f"ch:order:{order_id}"


def state_key(drone_id: UUID) -> str:
    return f"{STATE_KEY_PREFIX}{drone_id}"
