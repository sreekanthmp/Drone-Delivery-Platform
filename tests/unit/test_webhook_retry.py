"""The retry policy: what an outcome means, how long to wait, when to give up."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.models import WebhookDelivery
from app.webhooks.retry import RetryPolicy, SendResult, Verdict
from tests.conftest import T0
from tests.unit.test_webhook_delivery import order_event

V = Verdict
MIDDLE = 0.5  # an rng that adds no jitter


def result(
    code: int | None, *, retry_after: float | None = None, error: str | None = None
) -> SendResult:
    return SendResult(status_code=code, duration_ms=10, error=error, retry_after_s=retry_after)


@pytest.fixture
def delivery() -> WebhookDelivery:
    return WebhookDelivery.queue(order_event(), "mock")


class TestWhatAnAnswerMeans:
    @pytest.mark.parametrize("code", [200, 201, 202, 204, 299])
    def test_a_2xx_is_delivered(self, code: int) -> None:
        assert RetryPolicy().verdict(result(code)) is V.DELIVERED

    @pytest.mark.parametrize("code", [500, 501, 502, 503, 504, 599, 408, 429])
    def test_these_are_worth_trying_again(self, code: int) -> None:
        assert RetryPolicy().verdict(result(code)) is V.RETRY

    def test_no_answer_is_worth_trying_again(self) -> None:
        assert RetryPolicy().verdict(result(None, error="timeout")) is V.RETRY

    @pytest.mark.parametrize(
        "code",
        [100, 199, 300, 301, 302, 304, 307, 308, 400, 401, 403, 404, 405, 409, 410, 422, 451],
    )
    def test_everything_else_is_final(self, code: int) -> None:
        assert RetryPolicy().verdict(result(code)) is V.FAILED


class TestTheWait:
    def test_it_grows_by_the_factor_from_the_base(self) -> None:
        policy = RetryPolicy(base_s=5, factor=3, cap_s=100_000)

        waits = [policy.delay_s(n, rng=lambda: MIDDLE) for n in range(1, 6)]

        assert waits == [5, 15, 45, 135, 405]

    def test_it_stops_growing_at_the_cap(self) -> None:
        policy = RetryPolicy(base_s=5, factor=3, cap_s=100)

        assert [policy.delay_s(n, rng=lambda: MIDDLE) for n in (3, 4, 10, 50)] == [
            45,
            100,
            100,
            100,
        ]

    def test_jitter_spreads_it_by_the_given_fraction_either_way(self) -> None:
        policy = RetryPolicy(base_s=100, factor=1, jitter=0.2)

        assert policy.delay_s(1, rng=lambda: 0.0) == pytest.approx(80)
        assert policy.delay_s(1, rng=lambda: 0.5) == pytest.approx(100)
        assert policy.delay_s(1, rng=lambda: 0.999999) == pytest.approx(120, abs=0.01)

    def test_a_retry_after_that_is_longer_wins(self) -> None:
        assert RetryPolicy(base_s=5).delay_s(1, rng=lambda: MIDDLE, retry_after_s=90) == 90

    def test_a_retry_after_that_is_shorter_is_ignored(self) -> None:
        assert RetryPolicy(base_s=60).delay_s(1, rng=lambda: MIDDLE, retry_after_s=2) == 60

    def test_a_retry_after_is_capped(self) -> None:
        policy = RetryPolicy(base_s=5, cap_s=600)

        assert policy.delay_s(1, rng=lambda: MIDDLE, retry_after_s=86_400) == 600

    @pytest.mark.parametrize("junk", [0.0, -5.0, float("nan"), float("inf")])
    def test_a_nonsense_retry_after_is_ignored(self, junk: float) -> None:
        assert RetryPolicy(base_s=5).delay_s(1, rng=lambda: MIDDLE, retry_after_s=junk) == 5


class TestThePlan:
    def test_delivered(self, delivery: WebhookDelivery) -> None:
        plan = RetryPolicy().plan(delivery, result(200), now=T0, rng=lambda: MIDDLE)

        assert (plan.verdict, plan.reason, plan.retry_at) == (V.DELIVERED, None, None)

    def test_a_retry_says_when(self, delivery: WebhookDelivery) -> None:
        plan = RetryPolicy().plan(delivery, result(503), now=T0, rng=lambda: MIDDLE)

        assert plan.verdict is V.RETRY
        assert plan.retry_at == T0 + timedelta(seconds=5)
        assert plan.reason == "HTTP 503"

    def test_the_wait_follows_the_attempts_in_this_round(self, delivery: WebhookDelivery) -> None:
        third = delivery.retry(now=T0, at=T0, status_code=500, error="x").retry(
            now=T0, at=T0, status_code=500, error="x"
        )

        plan = RetryPolicy().plan(third, result(503), now=T0, rng=lambda: MIDDLE)

        assert plan.retry_at == T0 + timedelta(seconds=45)  # the third failure

    def test_a_requeued_delivery_starts_the_waits_again(self, delivery: WebhookDelivery) -> None:
        dead = delivery.retry(now=T0, at=T0, status_code=500, error="x").failed(now=T0, error="x")

        plan = RetryPolicy().plan(dead.requeued(now=T0), result(503), now=T0, rng=lambda: MIDDLE)

        assert plan.retry_at == T0 + timedelta(seconds=5)

    def test_the_last_attempt_fails_it(self, delivery: WebhookDelivery) -> None:
        policy = RetryPolicy(max_attempts=2)
        second = delivery.retry(now=T0, at=T0, status_code=500, error="x")

        plan = policy.plan(second, result(503), now=T0, rng=lambda: MIDDLE)

        assert plan.verdict is V.FAILED
        assert plan.reason == "gave up after 2 attempts: HTTP 503"
        assert plan.retry_at is None

    def test_a_single_attempt_policy_never_retries(self, delivery: WebhookDelivery) -> None:
        plan = RetryPolicy(max_attempts=1).plan(delivery, result(503), now=T0, rng=lambda: MIDDLE)

        assert plan.verdict is V.FAILED

    def test_a_client_error_fails_at_once_whatever_the_budget(
        self, delivery: WebhookDelivery
    ) -> None:
        plan = RetryPolicy(max_attempts=99).plan(delivery, result(404), now=T0, rng=lambda: MIDDLE)

        assert (plan.verdict, plan.reason) == (V.FAILED, "the partner rejected it: HTTP 404")

    def test_a_redirect_says_so(self, delivery: WebhookDelivery) -> None:
        plan = RetryPolicy().plan(delivery, result(301), now=T0, rng=lambda: MIDDLE)

        assert plan.verdict is V.FAILED
        assert plan.reason is not None
        assert "redirects are not followed" in plan.reason

    def test_no_answer_says_why(self, delivery: WebhookDelivery) -> None:
        plan = RetryPolicy().plan(
            delivery, result(None, error="connection failed"), now=T0, rng=lambda: MIDDLE
        )

        assert (plan.verdict, plan.reason) == (V.RETRY, "connection failed")

    def test_the_default_schedule_gives_up_after_about_an_hour_and_a_half(self) -> None:
        policy = RetryPolicy()  # 8 attempts, 5 s x3, capped at an hour

        waits = [policy.delay_s(n, rng=lambda: MIDDLE) for n in range(1, policy.max_attempts)]

        assert waits == [5, 15, 45, 135, 405, 1215, 3600]
        assert sum(waits) == 5420  # ~90 minutes


class TestTheSettingsAreChecked:
    @pytest.mark.parametrize(
        "bad",
        [
            {"max_attempts": 0},
            {"base_s": 0},
            {"base_s": float("nan")},
            {"cap_s": -1},
            {"factor": 0.5},
            {"factor": float("inf")},
            {"jitter": 1.0},
            {"jitter": -0.1},
        ],
    )
    def test_nonsense_is_refused(self, bad: dict[str, float]) -> None:
        with pytest.raises(ValueError, match=r"must be|at least|below"):
            RetryPolicy(**bad)  # type: ignore[arg-type]
