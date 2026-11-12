"""Pydantic helpers shared by the adapters' inbound payload models.

Inbound models are *tolerant readers* (unknown fields are ignored) but strict about
the fields they do read: no string-to-number coercion, no booleans-as-numbers, and no
NaN or infinity. Errors carry a path and a message and never echo the offending value.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
)

from app.adapters.base import AdapterValidationError, FieldIssue
from app.domain.models import AuditEvent, AuditEventType, Order, OrderStatus

MAX_ID_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 500

Latitude = Annotated[float, Strict(), Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Strict(), Field(ge=-180, le=180, allow_inf_nan=False)]
PositiveNumber = Annotated[float, Strict(), Field(gt=0, allow_inf_nan=False)]

# Surrounding whitespace is stripped, so " ORD-1 " and "ORD-1" are the same idempotency key.
PartnerId = Annotated[
    str, Strict(), StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ID_LENGTH)
]
Description = Annotated[str, Strict(), StringConstraints(max_length=MAX_DESCRIPTION_LENGTH)]


class PartnerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


def _format_path(location: tuple[int | str, ...]) -> str:
    path = ""
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path or "$"


def issues_from(error: ValidationError) -> tuple[FieldIssue, ...]:
    return tuple(
        FieldIssue(path=_format_path(item["loc"]), message=item["msg"])
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    )


def validate_payload[M: PartnerModel](model: type[M], payload: Mapping[str, Any]) -> M:
    """Validate ``payload`` against ``model``, raising :class:`AdapterValidationError`."""
    try:
        return model.model_validate(dict(payload))
    except ValidationError as error:
        raise AdapterValidationError(issues_from(error)) from error


def truncate_description(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_LENGTH:
        return text
    return text[: MAX_DESCRIPTION_LENGTH - 1] + "…"


def iso(moment: datetime) -> str:
    """ISO-8601 timestamp in UTC."""
    return moment.astimezone(UTC).isoformat()


_ORDER_EVENT_TYPES = frozenset({AuditEventType.ORDER_CREATED, AuditEventType.ORDER_STATUS_CHANGED})


def order_status_of(order: Order, event: AuditEvent) -> OrderStatus:
    """The order status an order audit event moved ``order`` into.

    Partners hear about *orders*. Flight events (which use a different status vocabulary
    that happens to share some strings, such as "cancelled") must never be translated as if
    they were order statuses, so anything else is rejected loudly.
    """
    if event.event_type not in _ORDER_EVENT_TYPES or not isinstance(event.new_state, OrderStatus):
        raise ValueError(
            f"only order status events can be rendered for partners, got '{event.event_type}'"
        )
    if event.order_id != order.id:
        raise ValueError("the event belongs to a different order")
    return event.new_state
