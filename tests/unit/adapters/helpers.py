"""Helpers shared by the adapter tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.adapters.base import AdapterValidationError, ClientAdapter
from app.domain.models import AuditEvent, AuditEventType, EventSource, Order, OrderStatus
from tests.conftest import T0

S = OrderStatus
MakeOrder = Callable[..., Order]


def event_for(order: Order, new_state: OrderStatus) -> AuditEvent:
    """An audit event that moved ``order`` into ``new_state``."""
    if new_state is S.CREATED:
        return AuditEvent(
            event_id=UUID(int=0xE1),
            order_id=order.id,
            event_type=AuditEventType.ORDER_CREATED,
            previous_state=None,
            new_state=S.CREATED,
            occurred_at=T0,
            source=EventSource.ADAPTER,
        )
    return AuditEvent(
        event_id=UUID(int=0xE1),
        order_id=order.id,
        event_type=AuditEventType.ORDER_STATUS_CHANGED,
        previous_state=S.CREATED,
        new_state=new_state,
        occurred_at=T0,
        source=EventSource.DISPATCH,
        drone_id=order.drone_id,
    )


def issue_paths(adapter: ClientAdapter, payload: dict[str, Any]) -> list[str]:
    """Parse ``payload`` expecting a validation failure; return the issue paths."""
    try:
        adapter.parse_order(payload)
    except AdapterValidationError as error:
        return [issue.path for issue in error.issues]
    raise AssertionError("expected the payload to be rejected")


def assert_json_serialisable(value: Any) -> None:
    """``json.dumps`` raises TypeError for UUIDs, datetimes, sets and mapping proxies."""
    json.dumps(value)
