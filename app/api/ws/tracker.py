"""Follows one order for a stream: its status, its flight, and the drone's position.

The platform knows the order's status from the database, which is always re-read (the live
message only says *that* something changed). A position is forwarded only if it comes from
the drone's sample **for this order's current flight**: a drone that has moved on to another
order, or is only flying home, must never show up as this order's position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.models import (
    LIVE_FLIGHT_STATES,
    AuditEventType,
    FlightStatus,
    Location,
    Order,
    OrderStatus,
    TrackingPosition,
)
from app.realtime.channels import drone_channel, order_channel
from app.realtime.models import DroneState, Envelope
from app.realtime.ports import StateStore
from app.services.order_service import OrderService

# The platform's own streams show the drone from the moment it is assigned; a partner sees it
# only while it is actually carrying the package.
PLATFORM_VISIBLE = frozenset(
    {
        OrderStatus.ASSIGNED,
        OrderStatus.PICKED_UP,
        OrderStatus.IN_FLIGHT,
        OrderStatus.ARRIVING,
    }
)
PARTNER_VISIBLE = frozenset({OrderStatus.PICKED_UP, OrderStatus.IN_FLIGHT, OrderStatus.ARRIVING})

_FLIGHT_OVER = frozenset(
    status.value for status in FlightStatus if status not in LIVE_FLIGHT_STATES
)


@dataclass(frozen=True, slots=True)
class Change:
    audit: dict[str, Any] | None = None
    status_changed: bool = False
    position_changed: bool = False
    new_channel: str | None = None
    """A channel the stream should start listening to (the drone just got assigned)."""


class OrderTracker:
    def __init__(
        self,
        orders: OrderService,
        order: Order,
        flight_id: UUID | None,
        state: DroneState | None,
        live: StateStore,
        *,
        visible: frozenset[OrderStatus],
    ) -> None:
        self._orders = orders
        self._live = live
        self._visible = visible
        self.order = order
        self.flight_id = flight_id
        self.state = state  # the latest sample for this order's current flight

    @classmethod
    async def load(
        cls,
        orders: OrderService,
        order: Order,
        live_flight_id: UUID | None,
        live: StateStore,
        *,
        visible: frozenset[OrderStatus],
    ) -> OrderTracker:
        state = None
        if order.drone_id is not None and live_flight_id is not None:
            sample = await live.get_state(order.drone_id)
            if sample is not None and sample.flight_id == live_flight_id:
                state = sample
        return cls(orders, order, live_flight_id, state, live, visible=visible)

    def channels(self) -> list[str]:
        found = [order_channel(self.order.id)]
        if self.order.drone_id is not None:
            found.append(drone_channel(self.order.drone_id))
        return found

    @property
    def position(self) -> TrackingPosition | None:
        """Where to show the drone right now, or ``None`` if this stream should not show it."""
        if self.state is None or self.order.status not in self._visible:
            return None
        return TrackingPosition(
            Location(latitude=self.state.latitude, longitude=self.state.longitude),
            self.state.altitude_m,
            self.state.received_at,
        )

    async def apply(self, channel: str, envelope: Envelope) -> Change:
        if envelope.kind == "audit":
            return await self._apply_audit(envelope.data)
        if envelope.kind == "drone_state":
            return self._apply_sample(envelope.data)
        return Change()

    async def _apply_audit(self, data: dict[str, Any]) -> Change:
        if data.get("order_id") != str(self.order.id):
            return Change()
        status_changed = False
        new_channel = None
        if data["event_type"] == AuditEventType.ORDER_STATUS_CHANGED.value:
            before = self.order.drone_id
            self.order = await self._orders.get_order(self.order.id)  # the database is the truth
            status_changed = True
            if self.order.drone_id is not None and self.order.drone_id != before:
                new_channel = drone_channel(self.order.drone_id)
        elif data["event_type"] in {
            AuditEventType.FLIGHT_CREATED.value,
            AuditEventType.FLIGHT_STATUS_CHANGED.value,
        }:
            if data["new_state"] in _FLIGHT_OVER:
                self.flight_id, self.state = None, None
            elif data["flight_id"] is not None:
                self.flight_id = UUID(data["flight_id"])
        position_changed = False
        if new_channel is not None:
            # Starting to follow the drone: show where it is now, not where it will be at its
            # next sample (anything published before we subscribed is gone).
            position_changed = await self._adopt_latest_sample()
        return Change(
            audit=data,
            status_changed=status_changed,
            position_changed=position_changed,
            new_channel=new_channel,
        )

    async def _adopt_latest_sample(self) -> bool:
        if self.order.drone_id is None or self.flight_id is None:
            return False
        try:
            sample = await self._live.get_state(self.order.drone_id)
        except Exception:
            return False  # the live view hiccupped: the next sample will do
        if sample is None or sample.flight_id != self.flight_id:
            return False
        self.state = sample
        return True

    def _apply_sample(self, data: dict[str, Any]) -> Change:
        sample = DroneState.model_validate(data)
        if (
            self.flight_id is None
            or sample.flight_id != self.flight_id
            or sample.drone_id != self.order.drone_id
        ):
            return Change()
        self.state = sample
        return Change(position_changed=True)
