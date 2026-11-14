"""The adapter contract and the neutral types adapters exchange with the API layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from app.domain.models import AuditEvent, Order, OrderRequest


class ErrorCode(StrEnum):
    """Platform-neutral error kinds. Each adapter renders them in its partner's format."""

    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    """The request is valid but the order is not in a state that allows it."""

    UNAVAILABLE = "unavailable"
    """Temporarily unable to do it (for example a drone that cannot be reached): retry later."""

    INTERNAL_ERROR = "internal_error"


HTTP_STATUS_FOR_ERROR: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


@dataclass(frozen=True, slots=True)
class FieldIssue:
    """One problem with a partner payload, located by a path such as ``items[1].weight_grams``."""

    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ClientError:
    code: ErrorCode
    message: str
    issues: tuple[FieldIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class ClientResponse:
    """What to send back to the partner: HTTP status and a JSON-serialisable body."""

    status_code: int
    body: Mapping[str, Any]


class AdapterValidationError(Exception):
    """The partner's payload is malformed or violates the partner's own contract."""

    def __init__(self, issues: Sequence[FieldIssue]) -> None:
        self.issues: tuple[FieldIssue, ...] = tuple(issues)
        summary = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues[:5])
        super().__init__(
            f"invalid order payload ({summary})" if summary else "invalid order payload"
        )


class ClientAdapter(Protocol):
    """Everything partner-specific lives behind this interface."""

    client_id: str
    """Stable lowercase identifier: it appears in URLs and is every order's ``client_id``."""

    def parse_order(self, payload: Mapping[str, Any]) -> OrderRequest:
        """Validate the partner's payload and map it to an :class:`OrderRequest`.

        Raises :class:`AdapterValidationError` for anything wrong with the payload. It must
        never let a raw validation or type error escape.
        """
        ...

    def render_order(self, order: Order, *, created: bool) -> ClientResponse:
        """The partner-format response for an order.

        ``created`` is False for a repeated request or a plain lookup.
        """
        ...

    def render_status_update(self, order: Order, event: AuditEvent) -> Mapping[str, Any]:
        """Translate an internal status change into the partner's status-update payload.

        Used by the webhook sender (a later phase) and, like every body here, must be
        JSON-serialisable.
        """
        ...

    def render_error(self, error: ClientError) -> ClientResponse:
        """The partner-format error response."""
        ...
