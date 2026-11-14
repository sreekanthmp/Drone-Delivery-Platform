"""The webhook sender: claims due deliveries, renders them for the partner, POSTs, records.

One pass (``run_once``):

1. claim a batch of due deliveries (a lease, so another sender leaves them alone);
2. for each, build the body with the partner's adapter (Rule 3: the partner's format is made
   by the partner's adapter and never appears here), sign it if the partner has a secret, POST
   it, and let the retry policy decide what the outcome means;
3. store the outcome, and the attempt, in one transaction.

Guarantees, and their limits:

* **At-least-once.** A sender that dies between the POST and step 3 leaves the delivery
  leased; when the lease expires it is sent again. Every attempt for an event carries the same
  ``X-Event-Id`` (the audit event's id) so the partner can drop a repeat.
* **In order per order.** The store hands out an order's updates one at a time, oldest first;
  a delivery that has failed for good no longer blocks the next one.
* **Never changes an order** (Rule 4) and never touches a drone (Rule 1): it reads orders and
  audit events and writes only the deliveries.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.adapters.registry import AdapterRegistry
from app.domain.models import AttemptOutcome, DeliveryAttempt, WebhookDelivery
from app.services.ports import UnitOfWorkFactory
from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.http import Poster
from app.webhooks.retry import Plan, RetryPolicy, SendResult, Verdict
from app.webhooks.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

logger = structlog.get_logger(__name__)

Clock = Callable[[], datetime]

USER_AGENT = "drone-delivery-platform-webhooks"
EVENT_ID_HEADER = "X-Event-Id"
ATTEMPT_HEADER = "X-Delivery-Attempt"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SenderConfig:
    batch_size: int = 20
    lease: timedelta = timedelta(seconds=60)
    concurrency: int = 10
    poll_s: float = 1.0

    def __post_init__(self) -> None:
        if self.batch_size < 1 or self.concurrency < 1:
            raise ValueError("batch_size and concurrency must be at least 1")
        if self.lease <= timedelta(0) or self.poll_s <= 0:
            raise ValueError("lease and poll_s must be positive")


class _Unsendable(Exception):
    """The delivery cannot even be turned into a request. Repeating it would not help."""


class WebhookSender:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        registry: AdapterRegistry,
        endpoints: Mapping[str, WebhookEndpoint],
        poster: Poster,
        policy: RetryPolicy | None = None,
        config: SenderConfig | None = None,
        clock: Clock = utc_now,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._endpoints = dict(endpoints)
        self._poster = poster
        self._policy = policy or RetryPolicy()
        self._config = config or SenderConfig()
        self._clock = clock
        self._rng = rng
        self.counts: Counter[str] = Counter()
        """What this sender has done: delivered, retry, failed, skipped, discarded."""

    # --- the loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        """Send until cancelled. Idle passes wait ``poll_s``; a busy one goes straight on."""
        while True:
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook_pass_failed")
                handled = 0
            if handled == 0:
                await asyncio.sleep(self._config.poll_s)

    async def run_once(self) -> int:
        """Claim a batch of due deliveries and handle each. Returns how many were claimed."""
        now = self._clock()
        async with self._uow_factory() as uow:
            claimed = await uow.webhooks.claim_due(
                now=now, lease_until=now + self._config.lease, limit=self._config.batch_size
            )
            await uow.commit()
        if not claimed:
            return 0

        gate = asyncio.Semaphore(self._config.concurrency)

        async def handle(delivery: WebhookDelivery) -> None:
            async with gate:
                try:
                    await self._deliver(delivery)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Most likely the database. The delivery stays leased and is sent again
                    # when the lease runs out: at-least-once, never lost.
                    logger.exception("webhook_delivery_failed", event_id=str(delivery.event_id))

        await asyncio.gather(*(handle(d) for d in claimed))
        return len(claimed)

    # --- one delivery -----------------------------------------------------------------------

    async def _deliver(self, delivery: WebhookDelivery) -> None:
        now = self._clock()
        endpoint = self._endpoints.get(delivery.client_id)
        if endpoint is None:
            await self._store(
                delivery,
                delivery.skipped(
                    now=now, reason="no webhook endpoint is configured for this client"
                ),
                None,
            )
            return

        try:
            body = await self._body_for(delivery)
        except _Unsendable as problem:
            await self._store(
                delivery, delivery.failed(now=now, error=str(problem), attempted=False), None
            )
            return

        attempt_no = delivery.attempts + 1
        started_at = self._clock()
        try:
            result = await self._poster.post(
                endpoint.url,
                body=body,
                headers=self._headers(delivery, endpoint, body, attempt_no, started_at),
            )
        except Exception as error:
            # A poster reports problems as results; one that raises has a bug. Treat it as a
            # failed attempt so the delivery is retried, not lost.
            logger.exception("webhook_poster_raised", event_id=str(delivery.event_id))
            result = SendResult(
                status_code=None, duration_ms=0, error=f"internal error ({type(error).__name__})"
            )

        finished = self._clock()
        plan = self._policy.plan(delivery, result, now=finished, rng=self._rng)
        after = self._apply_plan(delivery, plan, result, now=finished)
        outcome = {
            Verdict.DELIVERED: AttemptOutcome.DELIVERED,
            Verdict.RETRY: AttemptOutcome.RETRY,
            Verdict.FAILED: AttemptOutcome.FAILED,
        }[plan.verdict]
        attempt = DeliveryAttempt(
            event_id=delivery.event_id,
            attempt_no=attempt_no,
            started_at=started_at,
            duration_ms=result.duration_ms,
            outcome=outcome,
            status_code=result.status_code,
            error=plan.reason if plan.verdict is not Verdict.DELIVERED else None,
        )
        if await self._store(delivery, after, attempt):
            self._log(delivery, plan, result, attempt_no)

    @staticmethod
    def _apply_plan(
        delivery: WebhookDelivery, plan: Plan, result: SendResult, *, now: datetime
    ) -> WebhookDelivery:
        if plan.verdict is Verdict.DELIVERED:
            assert result.status_code is not None
            return delivery.delivered(now=now, status_code=result.status_code)
        reason = plan.reason or "failed"
        if plan.verdict is Verdict.RETRY:
            assert plan.retry_at is not None
            return delivery.retry(
                now=now, at=plan.retry_at, status_code=result.status_code, error=reason
            )
        return delivery.failed(now=now, error=reason, status_code=result.status_code)

    async def _body_for(self, delivery: WebhookDelivery) -> bytes:
        """The partner-format JSON for the delivery's event, made by the partner's adapter."""
        async with self._uow_factory() as uow:
            order = await uow.orders.get(delivery.order_id)
            event = await uow.audit.get(delivery.event_id)
        if order is None or event is None:
            raise _Unsendable("the order or its audit event no longer exists")
        try:
            adapter = self._registry.get(delivery.client_id)
        except LookupError as problem:
            raise _Unsendable(str(problem)) from problem
        try:
            payload = adapter.render_status_update(order, event)
            return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        except Exception as problem:
            logger.exception("webhook_render_failed", event_id=str(delivery.event_id))
            raise _Unsendable(f"cannot render the update ({type(problem).__name__})") from problem

    @staticmethod
    def _headers(
        delivery: WebhookDelivery,
        endpoint: WebhookEndpoint,
        body: bytes,
        attempt_no: int,
        at: datetime,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            EVENT_ID_HEADER: str(delivery.event_id),
            ATTEMPT_HEADER: str(attempt_no),
        }
        if endpoint.secret is not None:
            timestamp = int(at.timestamp())
            headers[TIMESTAMP_HEADER] = str(timestamp)
            headers[SIGNATURE_HEADER] = sign(endpoint.secret.get_secret_value(), timestamp, body)
        return headers

    async def _store(
        self, before: WebhookDelivery, after: WebhookDelivery, attempt: DeliveryAttempt | None
    ) -> bool:
        async with self._uow_factory() as uow:
            applied = await uow.webhooks.apply(before, after, attempt)
            if applied:
                await uow.commit()
        if not applied:
            # Another sender got there first (our lease had run out). Its record stands.
            self.counts["discarded"] += 1
            logger.warning("webhook_result_discarded", event_id=str(before.event_id))
            return False
        if attempt is None:
            self.counts[after.status.value] += 1
            logger.warning(
                "webhook_closed_without_sending",
                client_id=after.client_id,
                event_id=str(after.event_id),
                status=after.status.value,
                reason=after.last_error,
            )
        return True

    def _log(
        self, delivery: WebhookDelivery, plan: Plan, result: SendResult, attempt_no: int
    ) -> None:
        self.counts[plan.verdict.value] += 1
        fields = {
            "client_id": delivery.client_id,
            "event_id": str(delivery.event_id),
            "attempt": attempt_no,
            "status_code": result.status_code,
            "duration_ms": result.duration_ms,
        }
        if plan.verdict is Verdict.DELIVERED:
            logger.info("webhook_delivered", **fields)
        elif plan.verdict is Verdict.RETRY:
            logger.warning(
                "webhook_retry_scheduled",
                **fields,
                reason=plan.reason,
                retry_at=None if plan.retry_at is None else plan.retry_at.isoformat(),
            )
        else:
            logger.error("webhook_failed", **fields, reason=plan.reason)
