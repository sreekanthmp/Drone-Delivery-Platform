"""Progress reported by a drone, as the dispatch service understands it.

A drone tells the platform what happened; it never tells the platform what to *do*. Each
event below implies at most one change per entity, and the service applies it only if the
current state allows it (a repeat is "already applied", an event that does not fit is
rejected and logged, and nothing is ever invented to fill a gap).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.domain.models import OrderStatus


class ProgressEvent(StrEnum):
    PACKAGE_PICKED_UP = "package_picked_up"
    DEPARTED_PICKUP = "departed_pickup"
    ARRIVING_AT_DROPOFF = "arriving_at_dropoff"
    PACKAGE_DELIVERED = "package_delivered"
    MISSION_COMPLETED = "mission_completed"
    DOCKED = "docked"
    FAULT = "fault"


class ProgressOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    """A repeat of something already recorded (MQTT delivers at least once): nothing to do."""

    NOTED = "noted"
    """Recorded in the log only (a fault): nothing in the system changes."""

    REJECTED = "rejected"
    """It does not fit the current state (or names something unknown). Nothing was changed."""


@dataclass(frozen=True, slots=True)
class ProgressResult:
    outcome: ProgressOutcome
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is ProgressOutcome.APPLIED


# The order's journey: each reported event moves it exactly one step along the forward path.
ORDER_STEP: Final[dict[ProgressEvent, tuple[OrderStatus, OrderStatus]]] = {
    ProgressEvent.PACKAGE_PICKED_UP: (OrderStatus.ASSIGNED, OrderStatus.PICKED_UP),
    ProgressEvent.DEPARTED_PICKUP: (OrderStatus.PICKED_UP, OrderStatus.IN_FLIGHT),
    ProgressEvent.ARRIVING_AT_DROPOFF: (OrderStatus.IN_FLIGHT, OrderStatus.ARRIVING),
    ProgressEvent.PACKAGE_DELIVERED: (OrderStatus.ARRIVING, OrderStatus.DELIVERED),
}

FORWARD_PATH: Final = (
    OrderStatus.CREATED,
    OrderStatus.ASSIGNED,
    OrderStatus.PICKED_UP,
    OrderStatus.IN_FLIGHT,
    OrderStatus.ARRIVING,
    OrderStatus.DELIVERED,
)


def at_or_beyond(status: OrderStatus, target: OrderStatus) -> bool:
    """Has the order already reached ``target`` on its forward journey?"""
    return status in FORWARD_PATH and FORWARD_PATH.index(status) >= FORWARD_PATH.index(target)
