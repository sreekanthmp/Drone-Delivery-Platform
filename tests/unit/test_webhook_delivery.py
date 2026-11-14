"""The webhook delivery model: what each change records, and what it refuses."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from app.domain.exceptions import WebhookDeliveryError
from app.domain.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    DeliveryAttempt,
    DeliveryStatus,
    EventSource,
    FlightStatus,
    OrderStatus,
    WebhookDelivery,
)
from tests.conftest import DRONE_ID, FLIGHT_ID, ORDER_ID, T0

D = DeliveryStatus
EVENT_ID = UUID(int=0xE1)
LATER = T0 + timedelta(seconds=30)


def order_event(**overrides: object) -> AuditEvent:
    fields: dict[str, object] = {
        "event_id": EVENT_ID,
        "order_id": ORDER_ID,
        "event_type": AuditEventType.ORDER_CREATED,
        "previous_state": None,
        "new_state": OrderStatus.CREATED,
        "occurred_at": T0,
        "source": EventSource.API,
    }
    fields.update(overrides)
    return AuditEvent(**fields)  # type: ignore[arg-type]


@pytest.fixture
def delivery() -> WebhookDelivery:
    return WebhookDelivery.queue(order_event(), "mock")


class TestQueueing:
    def test_a_new_delivery_is_pending_and_due_when_the_event_happened(
        self, delivery: WebhookDelivery
    ) -> None:
        assert delivery.status is D.PENDING
        assert (delivery.event_id, delivery.order_id, delivery.client_id) == (
            EVENT_ID,
            ORDER_ID,
            "mock",
        )
        assert delivery.attempts == delivery.round_start == 0
        assert delivery.next_attempt_at == delivery.created_at == delivery.updated_at == T0
        assert delivery.seq is None
        assert delivery.lease_until is None

    def test_a_status_change_event_can_be_queued(self) -> None:
        event = order_event(
            event_type=AuditEventType.ORDER_STATUS_CHANGED,
            previous_state=OrderStatus.CREATED,
            new_state=OrderStatus.CANCELLED,
        )

        assert WebhookDelivery.queue(event, "mock").event_id == event.event_id

    @pytest.mark.parametrize(
        "event_type", [AuditEventType.FLIGHT_CREATED, AuditEventType.FLIGHT_STATUS_CHANGED]
    )
    def test_flight_events_are_never_reported_to_partners(self, event_type: AuditEventType) -> None:
        planned = event_type is AuditEventType.FLIGHT_CREATED
        event = order_event(
            event_type=event_type,
            previous_state=None if planned else FlightStatus.PLANNED,
            new_state=FlightStatus.PLANNED if planned else FlightStatus.CANCELLED,
            flight_id=FLIGHT_ID,
            drone_id=DRONE_ID,
        )

        with pytest.raises(WebhookDeliveryError, match="only order events"):
            WebhookDelivery.queue(event, "mock")


class TestTheOutcomes:
    def test_delivered(self, delivery: WebhookDelivery) -> None:
        done = delivery.delivered(now=LATER, status_code=204)

        assert done.status is D.DELIVERED
        assert (done.attempts, done.last_status_code, done.last_error) == (1, 204, None)
        assert done.delivered_at == done.updated_at == LATER
        assert done.is_final

    def test_a_retry_counts_the_attempt_and_sets_the_next_time(
        self, delivery: WebhookDelivery
    ) -> None:
        again = delivery.retry(now=T0, at=LATER, status_code=503, error="HTTP 503")

        assert again.status is D.PENDING
        assert (again.attempts, again.next_attempt_at) == (1, LATER)
        assert (again.last_status_code, again.last_error) == (503, "HTTP 503")
        assert not again.is_final

    def test_a_retry_cannot_be_scheduled_in_the_past(self, delivery: WebhookDelivery) -> None:
        with pytest.raises(WebhookDeliveryError, match="in the past"):
            delivery.retry(now=LATER, at=T0, status_code=None, error="x")

    def test_failed(self, delivery: WebhookDelivery) -> None:
        dead = delivery.failed(
            now=LATER, error="the partner rejected it: HTTP 400", status_code=400
        )

        assert (dead.status, dead.attempts, dead.last_status_code) == (D.FAILED, 1, 400)
        assert not dead.is_final  # operations may requeue it

    def test_failing_without_a_request_does_not_count_an_attempt(
        self, delivery: WebhookDelivery
    ) -> None:
        dead = delivery.failed(now=LATER, error="no adapter", attempted=False)

        assert (dead.status, dead.attempts) == (D.FAILED, 0)

    def test_skipped_is_final_and_makes_no_attempt(self, delivery: WebhookDelivery) -> None:
        skipped = delivery.skipped(now=LATER, reason="no endpoint")

        assert (skipped.status, skipped.attempts, skipped.last_error) == (
            D.SKIPPED,
            0,
            "no endpoint",
        )
        assert skipped.is_final

    def test_a_claim_sets_the_lease_and_nothing_else(self, delivery: WebhookDelivery) -> None:
        taken = delivery.claimed(until=LATER, now=T0)

        assert taken.lease_until == LATER
        assert replace(taken, lease_until=None, updated_at=delivery.updated_at) == delivery

    def test_every_change_clears_the_lease(self, delivery: WebhookDelivery) -> None:
        taken = delivery.claimed(until=LATER, now=T0)

        assert taken.delivered(now=T0, status_code=200).lease_until is None
        assert taken.retry(now=T0, at=LATER, status_code=500, error="x").lease_until is None
        assert taken.failed(now=T0, error="x").lease_until is None
        assert taken.skipped(now=T0, reason="x").lease_until is None

    def test_a_long_error_is_cut(self, delivery: WebhookDelivery) -> None:
        dead = delivery.failed(now=T0, error="x" * 5000)

        assert dead.last_error is not None
        assert len(dead.last_error) == 500
        assert dead.last_error.endswith("…")


class TestWhatIsRefused:
    @pytest.mark.parametrize("final", ["delivered", "failed", "skipped"])
    @pytest.mark.parametrize("change", ["delivered", "retry", "failed", "skipped", "claimed"])
    def test_only_a_pending_delivery_can_change(
        self, delivery: WebhookDelivery, final: str, change: str
    ) -> None:
        settled = {
            "delivered": delivery.delivered(now=T0, status_code=200),
            "failed": delivery.failed(now=T0, error="x"),
            "skipped": delivery.skipped(now=T0, reason="x"),
        }[final]
        attempt = {
            "delivered": lambda: settled.delivered(now=LATER, status_code=200),
            "retry": lambda: settled.retry(now=T0, at=LATER, status_code=None, error="x"),
            "failed": lambda: settled.failed(now=LATER, error="x"),
            "skipped": lambda: settled.skipped(now=LATER, reason="x"),
            "claimed": lambda: settled.claimed(until=LATER, now=T0),
        }[change]

        with pytest.raises(WebhookDeliveryError, match=final):
            attempt()

    def test_only_a_failed_delivery_can_be_requeued(self, delivery: WebhookDelivery) -> None:
        for not_failed in (
            delivery,
            delivery.delivered(now=T0, status_code=200),
            delivery.skipped(now=T0, reason="x"),
        ):
            with pytest.raises(WebhookDeliveryError, match="only a failed delivery"):
                not_failed.requeued(now=LATER)

    def test_a_requeue_starts_a_new_round_without_forgetting_the_history(
        self, delivery: WebhookDelivery
    ) -> None:
        dead = delivery.retry(now=T0, at=T0, status_code=503, error="x").failed(
            now=T0, error="gave up"
        )

        again = dead.requeued(now=LATER)

        assert again.status is D.PENDING
        assert (again.attempts, again.round_start, again.attempts_this_round) == (2, 2, 0)
        assert again.next_attempt_at == LATER
        assert again.last_error == "gave up"  # kept until the next attempt replaces it


class TestInvariants:
    @pytest.mark.parametrize(
        "change",
        [
            {"attempts": -1},
            {"round_start": 1},  # more than attempts
            {"last_status_code": 99},
            {"last_status_code": 600},
            {"client_id": ""},
            {"status": D.DELIVERED},  # delivered without delivered_at
            {"delivered_at": T0},  # delivered_at without being delivered
            {"status": D.FAILED, "lease_until": T0},  # only a pending delivery can be leased
        ],
    )
    def test_nonsense_is_refused(
        self, delivery: WebhookDelivery, change: dict[str, object]
    ) -> None:
        with pytest.raises(WebhookDeliveryError):
            replace(delivery, **change)

    def test_times_must_be_timezone_aware(self, delivery: WebhookDelivery) -> None:
        with pytest.raises(WebhookDeliveryError, match="timezone-aware"):
            replace(delivery, next_attempt_at=T0.replace(tzinfo=None))


class TestAnAttempt:
    def test_it_records_what_happened(self) -> None:
        attempt = DeliveryAttempt(
            EVENT_ID, 1, T0, 250, AttemptOutcome.RETRY, status_code=503, error="HTTP 503"
        )

        assert (attempt.attempt_no, attempt.duration_ms, attempt.status_code) == (1, 250, 503)

    @pytest.mark.parametrize(
        "change",
        [{"attempt_no": 0}, {"duration_ms": -1}, {"status_code": 42}],
    )
    def test_nonsense_is_refused(self, change: dict[str, object]) -> None:
        fields: dict[str, object] = {
            "event_id": EVENT_ID,
            "attempt_no": 1,
            "started_at": T0,
            "duration_ms": 1,
            "outcome": AttemptOutcome.DELIVERED,
        }
        fields.update(change)
        with pytest.raises(WebhookDeliveryError):
            DeliveryAttempt(**fields)  # type: ignore[arg-type]

    def test_a_long_error_is_cut(self) -> None:
        attempt = DeliveryAttempt(EVENT_ID, 1, T0, 1, AttemptOutcome.RETRY, error="y" * 900)

        assert attempt.error is not None
        assert len(attempt.error) == 500
