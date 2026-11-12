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
