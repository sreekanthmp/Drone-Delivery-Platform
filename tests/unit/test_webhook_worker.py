"""The webhook sender process: what it will and will not start with, and how it is wired."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.registry import AdapterRegistry, default_registry
from app.config import Settings
from app.domain.models import DeliveryStatus
from app.services.memory import InMemoryDatabase, InMemoryUnitOfWork
from app.webhook_worker import (
    WebhookConfigError,
    build_sender,
    check_endpoints,
    retry_policy_from,
    run_sender,
    sender_config_from,
)
from app.webhooks.endpoints import WebhookEndpoint
from tests.webhook_rig import PARTNER_URL, FakePoster, build_webhook_rig

ROOT = Path(__file__).resolve().parents[2]


def settings(**overrides: object) -> Settings:
    overrides.setdefault("webhook_endpoints", {"mock": {"url": PARTNER_URL}})
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


class TestWhatItWillNotStartWith:
    def test_no_endpoints_at_all(self) -> None:
        """It would mark every delivery skipped, and skipped is final."""
        with pytest.raises(WebhookConfigError, match="skipped is final"):
            check_endpoints(settings(webhook_endpoints={}), default_registry())

    def test_an_endpoint_for_a_client_with_no_adapter(self) -> None:
        both = settings(
            webhook_endpoints={"mock": {"url": PARTNER_URL}, "nobody": {"url": PARTNER_URL}}
        )

        with pytest.raises(WebhookConfigError, match="no adapter: nobody"):
            check_endpoints(both, default_registry())

    def test_a_registry_with_nothing_in_it(self) -> None:
        with pytest.raises(WebhookConfigError, match="no adapter: mock"):
            check_endpoints(settings(), AdapterRegistry([]))

    def test_one_known_client_is_enough(self) -> None:
        check_endpoints(settings(), default_registry())  # no error

    def test_a_partner_without_an_entry_is_fine(self) -> None:
        """Skipped on purpose: it has not asked to be told. Only *no* endpoints is refused."""
        check_endpoints(
            settings(webhook_endpoints={"zomato": {"url": PARTNER_URL}}), default_registry()
        )


class TestTheWiring:
    def test_the_retry_policy_comes_from_the_settings(self) -> None:
        policy = retry_policy_from(
            settings(
                webhook_max_attempts=3,
                webhook_backoff_base_seconds=2,
                webhook_backoff_factor=4,
                webhook_backoff_cap_seconds=100,
                webhook_backoff_jitter=0.5,
            )
        )

        assert (policy.max_attempts, policy.base_s, policy.factor, policy.cap_s, policy.jitter) == (
            3,
            2,
            4,
            100,
            0.5,
        )

    def test_the_sender_config_comes_from_the_settings(self) -> None:
        config = sender_config_from(
            settings(
                webhook_batch_size=7,
                webhook_lease_seconds=90,
                webhook_concurrency=3,
                webhook_poll_seconds=0.25,
            )
        )

        assert (config.batch_size, config.lease, config.concurrency, config.poll_s) == (
            7,
            timedelta(seconds=90),
            3,
            0.25,
        )

    async def test_the_sender_sends_to_the_configured_endpoints_with_the_configured_secret(
        self,
    ) -> None:
        database = InMemoryDatabase()
        factory = lambda: InMemoryUnitOfWork(database)  # noqa: E731
        poster = FakePoster()
        sender = build_sender(
            settings(
                webhook_endpoints={
                    "mock": {"url": "https://a.example/h", "secret": "s3cret"},
                }
            ),
            factory,
            poster,
        )
        rig = build_webhook_rig(factory, poster=poster)
        await rig.new_order()

        await sender.run_once()

        (call,) = poster.calls
        assert call.url == "https://a.example/h"
        assert "X-Signature" in call.headers

    def test_it_will_not_build_a_sender_it_would_not_start(self) -> None:
        with pytest.raises(WebhookConfigError):
            build_sender(
                settings(webhook_endpoints={}),
                lambda: InMemoryUnitOfWork(InMemoryDatabase()),
                FakePoster(),
            )


class TestRunning:
    async def test_it_sends_until_told_to_stop_and_then_stops(self) -> None:
        database = InMemoryDatabase()
        factory = lambda: InMemoryUnitOfWork(database)  # noqa: E731
        poster = FakePoster()
        rig = build_webhook_rig(factory, poster=poster)
        stop = asyncio.Event()

        task = asyncio.create_task(
            run_sender(
                settings(webhook_poll_seconds=0.01), stop, uow_factory=factory, poster=poster
            )
        )
        await rig.new_order()
        async with asyncio.timeout(5):
            while not poster.calls:
                await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        (delivery,) = await rig.deliveries()
        assert delivery.status is DeliveryStatus.DELIVERED

    async def test_it_refuses_to_run_misconfigured(self) -> None:
        stop = asyncio.Event()
        factory = lambda: InMemoryUnitOfWork(InMemoryDatabase())  # noqa: E731

        with pytest.raises(WebhookConfigError):
            await run_sender(
                settings(webhook_endpoints={}), stop, uow_factory=factory, poster=FakePoster()
            )


class TestTheCommand:
    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        import os

        return subprocess.run(
            [sys.executable, "-m", "app.webhooks"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "WEBHOOK_ENDPOINTS": "", **env},
        )

    def test_without_endpoints_it_says_why_and_exits_with_2(self) -> None:
        result = self.run()

        assert result.returncode == 2
        assert "WEBHOOK_ENDPOINTS" in result.stderr
        assert "Traceback" not in result.stderr

    def test_with_an_unknown_client_it_names_the_client_and_exits_with_2(self) -> None:
        result = self.run(WEBHOOK_ENDPOINTS='{"nobody": {"url": "https://a.example/h"}}')

        assert result.returncode == 2
        assert "nobody" in result.stderr
        assert "Traceback" not in result.stderr

    def test_a_secret_never_appears_in_what_it_prints(self) -> None:
        result = self.run(
            WEBHOOK_ENDPOINTS='{"nobody": {"url": "https://a.example/h", "secret": "TOPSECRET"}}'
        )

        assert "TOPSECRET" not in result.stdout + result.stderr


def test_the_endpoint_model_is_what_the_settings_hold() -> None:
    assert isinstance(settings().webhook_endpoints["mock"], WebhookEndpoint)
