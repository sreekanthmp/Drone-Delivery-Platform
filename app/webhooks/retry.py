"""When a webhook attempt counts as delivered, when to try again, and when to give up.

Pure decisions over an HTTP outcome; no I/O and no clock of its own. ``RetryPolicy.plan`` is
the one place that classifies a response:

* a 2xx is delivered;
* a network error, a timeout, a 5xx, 408 or 429 is worth repeating (the partner may recover);
* anything else (another 4xx, or a redirect, which is never followed) is permanent: repeating
  the same request would get the same answer.

Waits grow as ``base * factor ** (n - 1)`` up to ``cap``, with random jitter so a recovering
partner is not hit by every retry at once. A ``Retry-After`` the partner sent is honoured (up
to ``cap``). After ``max_attempts`` attempts in a round the delivery fails.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.models import WebhookDelivery

RETRYABLE_STATUSES = frozenset({408, 429})


class Verdict(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    """What came back from one POST. ``status_code`` is ``None`` when no response arrived."""

    status_code: int | None
    duration_ms: int
    error: str | None = None
    retry_after_s: float | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """What to do about an attempt: the verdict, and for a retry when to make it."""

    verdict: Verdict
    reason: str | None = None
    retry_at: datetime | None = None


def _finite_positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 8
    base_s: float = 5.0
    factor: float = 3.0
    cap_s: float = 3600.0
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        _finite_positive("base_s", self.base_s)
        _finite_positive("cap_s", self.cap_s)
        if not math.isfinite(self.factor) or self.factor < 1:
            raise ValueError("factor must be a finite number of at least 1")
        if not math.isfinite(self.jitter) or not 0 <= self.jitter < 1:
            raise ValueError("jitter must be at least 0 and below 1")

    def verdict(self, result: SendResult) -> Verdict:
        code = result.status_code
        if code is None:
            return Verdict.RETRY
        if 200 <= code < 300:
            return Verdict.DELIVERED
        if code >= 500 or code in RETRYABLE_STATUSES:
            return Verdict.RETRY
        return Verdict.FAILED

    def delay_s(
        self,
        failures_in_round: int,
        *,
        rng: Callable[[], float],
        retry_after_s: float | None = None,
    ) -> float:
        """Seconds to wait after the ``failures_in_round``-th failed attempt (1-based)."""
        backoff = min(self.cap_s, self.base_s * self.factor ** (failures_in_round - 1))
        backoff *= 1 + self.jitter * (2 * rng() - 1)
        if retry_after_s is not None and math.isfinite(retry_after_s) and retry_after_s > 0:
            backoff = max(backoff, min(retry_after_s, self.cap_s))
        return backoff

    def plan(
        self,
        delivery: WebhookDelivery,
        result: SendResult,
        *,
        now: datetime,
        rng: Callable[[], float],
    ) -> Plan:
        """Decide what happens to ``delivery`` given the attempt ``result`` made at ``now``."""
        verdict = self.verdict(result)
        code = result.status_code
        if verdict is Verdict.DELIVERED:
            return Plan(Verdict.DELIVERED)

        if code is None:
            what = result.error or "no response"
        elif 300 <= code < 400:
            what = f"HTTP {code} (redirects are not followed)"
        else:
            what = f"HTTP {code}"

        if verdict is Verdict.FAILED:
            return Plan(Verdict.FAILED, reason=f"the partner rejected it: {what}")

        failures = delivery.attempts_this_round + 1
        if failures >= self.max_attempts:
            return Plan(
                Verdict.FAILED, reason=f"gave up after {self.max_attempts} attempts: {what}"
            )
        wait = self.delay_s(failures, rng=rng, retry_after_s=result.retry_after_s)
        return Plan(Verdict.RETRY, reason=what, retry_at=now + timedelta(seconds=wait))
