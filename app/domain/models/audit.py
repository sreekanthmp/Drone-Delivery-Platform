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
from app.domain.models.order import OrderStatus


class AuditEventType(StrEnum):
    ORDER_CREATED = "order_created"
    ORDER_STATUS_CHANGED = "order_status_changed"
    # Flight event types are added in the dispatch/compliance phase.


class EventSource(StrEnum):
    """Which part of the platform caused the event."""

    API = "api"
    ADAPTER = "adapter"
    DISPATCH = "dispatch"
    TELEMETRY = "telemetry"
    WEBHOOK = "webhook"
    OPS = "ops"
    SYSTEM = "system"


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
    previous_state: OrderStatus | None
    new_state: OrderStatus | None
    occurred_at: datetime
    source: EventSource
    drone_id: UUID | None = None
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
            case AuditEventType.ORDER_STATUS_CHANGED:
                if self.previous_state is None or self.new_state is None:
                    raise AuditEventValidationError(
                        "order_status_changed events need previous_state and new_state"
                    )
                if self.previous_state == self.new_state:
                    raise AuditEventValidationError(
                        "order_status_changed events must change the state"
                    )

        if not isinstance(self.metadata, Mapping):
            raise AuditEventValidationError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "metadata"))
