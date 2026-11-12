"""Audit event model (Rule 5).

An :class:`AuditEvent` is an immutable fact. The domain only *produces* events;
persisting them append-only is the database layer's job (Phase 2).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from app.domain.exceptions import AuditEventValidationError
from app.domain.models.flight import FlightStatus
from app.domain.models.order import OrderStatus


class AuditEventType(StrEnum):
    ORDER_CREATED = "order_created"
    ORDER_STATUS_CHANGED = "order_status_changed"
    FLIGHT_CREATED = "flight_created"
    FLIGHT_STATUS_CHANGED = "flight_status_changed"


class EventSource(StrEnum):
    """Which part of the platform caused the event."""

    API = "api"
    ADAPTER = "adapter"
    DISPATCH = "dispatch"
    TELEMETRY = "telemetry"
    WEBHOOK = "webhook"
    OPS = "ops"
    SYSTEM = "system"


AuditState = OrderStatus | FlightStatus
"""Order events use :class:`OrderStatus`; flight events use :class:`FlightStatus`."""


def _freeze(value: object, path: str) -> object:
    """Deep-copy ``value`` into an immutable, JSON-compatible structure.

    Metadata is persisted as JSON, so only ``None``, bool, int, finite float,
    str, lists/tuples and string-keyed mappings are accepted. Callers should
    convert UUIDs, datetimes and the like to strings themselves.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditEventValidationError(f"{path} must be a finite number, got {value}")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditEventValidationError(f"{path} has a non-string key: {key!r}")
            frozen[key] = _freeze(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item, f"{path}[{index}]") for index, item in enumerate(value))
    raise AuditEventValidationError(f"{path} has unsupported type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: UUID
    order_id: UUID
    event_type: AuditEventType
    previous_state: AuditState | None
    new_state: AuditState | None
    occurred_at: datetime
    source: EventSource
    drone_id: UUID | None = None
    flight_id: UUID | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise AuditEventValidationError("occurred_at must be timezone-aware")

        match self.event_type:
            case AuditEventType.ORDER_CREATED:
                if self.previous_state is not None or self.new_state is not OrderStatus.CREATED:
                    raise AuditEventValidationError(
                        "order_created events must go from no state to 'created'"
                    )
                self._require_no_flight()
            case AuditEventType.ORDER_STATUS_CHANGED:
                if not (
                    isinstance(self.previous_state, OrderStatus)
                    and isinstance(self.new_state, OrderStatus)
                ):
                    raise AuditEventValidationError(
                        "order_status_changed events need an order previous_state and new_state"
                    )
                if self.previous_state == self.new_state:
                    raise AuditEventValidationError(
                        "order_status_changed events must change the state"
                    )
                self._require_no_flight()
            case AuditEventType.FLIGHT_CREATED:
                if self.previous_state is not None or self.new_state is not FlightStatus.PLANNED:
                    raise AuditEventValidationError(
                        "flight_created events must go from no state to 'planned'"
                    )
                self._require_flight_and_drone()
            case AuditEventType.FLIGHT_STATUS_CHANGED:
                if not (
                    isinstance(self.previous_state, FlightStatus)
                    and isinstance(self.new_state, FlightStatus)
                ):
                    raise AuditEventValidationError(
                        "flight_status_changed events need a flight previous_state and new_state"
                    )
                if self.previous_state == self.new_state:
                    raise AuditEventValidationError(
                        "flight_status_changed events must change the state"
                    )
                self._require_flight_and_drone()

        if not isinstance(self.metadata, Mapping):
            raise AuditEventValidationError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "metadata"))

    def _require_no_flight(self) -> None:
        if self.flight_id is not None:
            raise AuditEventValidationError("order events do not belong to a flight")

    def _require_flight_and_drone(self) -> None:
        if self.flight_id is None or self.drone_id is None:
            raise AuditEventValidationError("flight events need a flight_id and a drone_id")
