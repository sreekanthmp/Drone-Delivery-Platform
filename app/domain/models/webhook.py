"""Webhook deliveries: telling a partner that an order's status changed.

One :class:`WebhookDelivery` exists per order audit event, and it *is* that event's
notification to the partner: it is identified by the event's id (which is also the
idempotency key the partner sees), so an event is queued at most once. A delivery is a plain
immutable value; each change returns a new one, like every other entity here. The decisions
(is this response a success, when to retry) live in ``app/webhooks/retry.py``; this module only
knows which changes are legal and what each one records.

Lifecycle::

    pending --delivered--> delivered
    pending --retry------> pending          (attempts + 1, a later next_attempt_at)
    pending --failed-----> failed            (permanent error, or retries used up)
    pending --skipped----> skipped           (the partner has no webhook endpoint configured)
    failed  --requeued---> pending           (operations asked for another round)

``delivered`` and ``skipped`` are final. Nothing here touches an order: a delivery reports a
change, it never causes one (Rule 4).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.exceptions import WebhookDeliveryError
from app.domain.models.audit import AuditEvent, AuditEventType

MAX_ERROR_LENGTH = 500
"""A stored error is cut to this length: it is a diagnostic, not a log of a response body."""

_ORDER_EVENT_TYPES = frozenset({AuditEventType.ORDER_CREATED, AuditEventType.ORDER_STATUS_CHANGED})


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


class AttemptOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"


def _clip(error: str | None) -> str | None:
    if error is None:
        return None
    return error if len(error) <= MAX_ERROR_LENGTH else error[: MAX_ERROR_LENGTH - 1] + "…"


def _require_aware(moment: datetime, name: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise WebhookDeliveryError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    event_id: UUID
    """The order audit event being reported. Also the delivery's identity and idempotency key."""

    client_id: str
    order_id: UUID
    status: DeliveryStatus
    attempts: int
    """HTTP attempts made so far, over the delivery's whole life."""

    round_start: int
    """``attempts`` when the current round of retries began (0, or higher after a requeue)."""

    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
    seq: int | None = None
    """Insertion order, given by the store; later events of an order have larger values."""

    lease_until: datetime | None = None
    """While set and in the future, a sender has claimed this delivery."""

    last_status_code: int | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("next_attempt_at", "created_at", "updated_at"):
            _require_aware(getattr(self, name), name)
        if self.lease_until is not None:
            _require_aware(self.lease_until, "lease_until")
        if self.delivered_at is not None:
            _require_aware(self.delivered_at, "delivered_at")
        if not self.client_id:
            raise WebhookDeliveryError("client_id must not be empty")
        if self.attempts < 0 or not 0 <= self.round_start <= self.attempts:
            raise WebhookDeliveryError(
                f"attempts ({self.attempts}) and round_start ({self.round_start}) are inconsistent"
            )
        if self.last_status_code is not None and not 100 <= self.last_status_code <= 599:
            raise WebhookDeliveryError(f"{self.last_status_code} is not an HTTP status code")
        if (self.status is DeliveryStatus.DELIVERED) != (self.delivered_at is not None):
            raise WebhookDeliveryError("delivered_at is set exactly when the status is delivered")
        if self.status is not DeliveryStatus.PENDING and self.lease_until is not None:
            raise WebhookDeliveryError("only a pending delivery can be leased")

    # --- creation ---------------------------------------------------------------------------

    @classmethod
    def queue(cls, event: AuditEvent, client_id: str) -> WebhookDelivery:
        """The delivery for an order audit event, due immediately.

        Only order events are reported to partners; flight events never are.
        """
        if event.event_type not in _ORDER_EVENT_TYPES:
            raise WebhookDeliveryError(
                f"only order events are reported to partners, got '{event.event_type}'"
            )
        return cls(
            event_id=event.event_id,
            client_id=client_id,
            order_id=event.order_id,
            status=DeliveryStatus.PENDING,
            attempts=0,
            round_start=0,
            next_attempt_at=event.occurred_at,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )

    # --- reading ----------------------------------------------------------------------------

    @property
    def attempts_this_round(self) -> int:
        return self.attempts - self.round_start

    @property
    def is_final(self) -> bool:
        return self.status in (DeliveryStatus.DELIVERED, DeliveryStatus.SKIPPED)

    # --- changes (each returns a new delivery) ----------------------------------------------

    def _require_pending(self, what: str) -> None:
        if self.status is not DeliveryStatus.PENDING:
            raise WebhookDeliveryError(
                f"cannot record {what} for a delivery that is {self.status.value}"
            )

    def delivered(self, *, now: datetime, status_code: int) -> WebhookDelivery:
        """The partner accepted it."""
        self._require_pending("a delivery")
        _require_aware(now, "now")
        return replace(
            self,
            status=DeliveryStatus.DELIVERED,
            attempts=self.attempts + 1,
            lease_until=None,
            last_status_code=status_code,
            last_error=None,
            delivered_at=now,
            updated_at=now,
        )

    def retry(
        self, *, now: datetime, at: datetime, status_code: int | None, error: str
    ) -> WebhookDelivery:
        """The attempt failed in a way worth repeating: try again at ``at``."""
        self._require_pending("a retry")
        _require_aware(now, "now")
        _require_aware(at, "at")
        if at < now:
            raise WebhookDeliveryError("a retry cannot be scheduled in the past")
        return replace(
            self,
            attempts=self.attempts + 1,
            next_attempt_at=at,
            lease_until=None,
            last_status_code=status_code,
            last_error=_clip(error),
            updated_at=now,
        )

    def failed(
        self,
        *,
        now: datetime,
        error: str,
        status_code: int | None = None,
        attempted: bool = True,
    ) -> WebhookDelivery:
        """Give up. ``attempted`` is False when no HTTP request was made (it could not be built)."""
        self._require_pending("a failure")
        _require_aware(now, "now")
        return replace(
            self,
            status=DeliveryStatus.FAILED,
            attempts=self.attempts + (1 if attempted else 0),
            lease_until=None,
            last_status_code=status_code,
            last_error=_clip(error),
            updated_at=now,
        )

    def skipped(self, *, now: datetime, reason: str) -> WebhookDelivery:
        """Nothing to send it to: the partner has no webhook endpoint."""
        self._require_pending("a skip")
        _require_aware(now, "now")
        return replace(
            self,
            status=DeliveryStatus.SKIPPED,
            lease_until=None,
            last_error=_clip(reason),
            updated_at=now,
        )

    def requeued(self, *, now: datetime) -> WebhookDelivery:
        """Operations asked for another round of attempts at a failed delivery."""
        if self.status is not DeliveryStatus.FAILED:
            raise WebhookDeliveryError(
                f"only a failed delivery can be requeued, this one is {self.status.value}"
            )
        _require_aware(now, "now")
        return replace(
            self,
            status=DeliveryStatus.PENDING,
            round_start=self.attempts,
            next_attempt_at=now,
            lease_until=None,
            updated_at=now,
        )

    def claimed(self, *, until: datetime, now: datetime) -> WebhookDelivery:
        """A sender took it, and nobody else may until ``until``."""
        self._require_pending("a claim")
        _require_aware(until, "until")
        _require_aware(now, "now")
        return replace(self, lease_until=until, updated_at=now)


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """One HTTP attempt at a delivery. Append-only history, kept for failure tracking."""

    event_id: UUID
    attempt_no: int
    """1-based, over the delivery's whole life (a requeue does not restart it)."""

    started_at: datetime
    duration_ms: int
    outcome: AttemptOutcome
    status_code: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.started_at, "started_at")
        if self.attempt_no < 1:
            raise WebhookDeliveryError("attempt_no starts at 1")
        if self.duration_ms < 0:
            raise WebhookDeliveryError("duration_ms must not be negative")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise WebhookDeliveryError(f"{self.status_code} is not an HTTP status code")
        object.__setattr__(self, "error", _clip(self.error))
