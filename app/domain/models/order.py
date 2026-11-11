"""Internal order model.

This is the platform's own representation of a delivery order. It knows
nothing about any partner's payload format (Rule 3) and has no persistence
concerns. Orders are immutable: the only way to obtain an order with a
different status is :class:`app.domain.order_state_machine.OrderStateMachine`
(Rule 4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Self
from uuid import UUID

from app.domain.exceptions import OrderValidationError


class OrderStatus(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    IN_FLIGHT = "in_flight"
    ARRIVING = "arriving"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETURNED = "returned"

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES


ACTIVE_STATES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.ASSIGNED,
        OrderStatus.PICKED_UP,
        OrderStatus.IN_FLIGHT,
        OrderStatus.ARRIVING,
    }
)
"""States from which an order can still fail or be cancelled."""

TERMINAL_STATES: Final[frozenset[OrderStatus]] = frozenset(OrderStatus) - ACTIVE_STATES
"""States with no outgoing transitions: delivered, failed, cancelled, returned."""

# Once an order has left ``created`` it has a drone attached, except that an
# order may still fail or be cancelled without one having been assigned.
_DRONE_REQUIRED: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.ASSIGNED,
        OrderStatus.PICKED_UP,
        OrderStatus.IN_FLIGHT,
        OrderStatus.ARRIVING,
        OrderStatus.DELIVERED,
        OrderStatus.RETURNED,
    }
)


@dataclass(frozen=True, slots=True)
class Location:
    """A WGS84 coordinate pair."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.latitude) and -90.0 <= self.latitude <= 90.0):
            raise OrderValidationError(f"latitude must be within [-90, 90], got {self.latitude}")
        if not (math.isfinite(self.longitude) and -180.0 <= self.longitude <= 180.0):
            raise OrderValidationError(
                f"longitude must be within [-180, 180], got {self.longitude}"
            )


@dataclass(frozen=True, slots=True)
class Package:
    """What is being delivered. Per-drone payload limits are a dispatch concern."""

    weight_kg: float
    description: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.weight_kg) or self.weight_kg <= 0:
            raise OrderValidationError(
                f"package weight must be a positive number of kg, got {self.weight_kg}"
            )


@dataclass(frozen=True, slots=True)
class Order:
    """A delivery order in the platform's internal format.

    ``(client_id, external_order_id)`` identifies the order in the partner's
    system and is the idempotency key for order intake.
    """

    id: UUID
    client_id: str
    external_order_id: str
    pickup: Location
    dropoff: Location
    package: Package
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    drone_id: UUID | None = None

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise OrderValidationError("client_id must not be blank")
        if not self.external_order_id.strip():
            raise OrderValidationError("external_order_id must not be blank")
        if self.pickup == self.dropoff:
            raise OrderValidationError("pickup and dropoff locations must differ")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise OrderValidationError(f"{name} must be timezone-aware")
        if self.status is OrderStatus.CREATED and self.drone_id is not None:
            raise OrderValidationError("a 'created' order cannot have a drone assigned")
        if self.status in _DRONE_REQUIRED and self.drone_id is None:
            raise OrderValidationError(f"an order in state '{self.status}' requires a drone_id")

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        client_id: str,
        external_order_id: str,
        pickup: Location,
        dropoff: Location,
        package: Package,
        status: OrderStatus,
        created_at: datetime,
        updated_at: datetime,
        drone_id: UUID | None = None,
    ) -> Self:
        """Rebuild an order that already exists, in whatever state it is in.

        **For persistence code only.** It re-validates invariants but performs no
        transition and emits no audit event, so it must never be used to
        change an order's status. New orders come from
        ``OrderStateMachine.create`` and changes from ``OrderStateMachine.transition``.
        """
        return cls(
            id=id,
            client_id=client_id,
            external_order_id=external_order_id,
            pickup=pickup,
            dropoff=dropoff,
            package=package,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            drone_id=drone_id,
        )
