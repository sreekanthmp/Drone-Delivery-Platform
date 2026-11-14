"""The webhook sender process: tells partners about order status changes.

Run with::

    python -m app.webhooks

It is a composition root like ``app.main`` and ``app.worker``, so it may import across layers.
It owns no drones and no orders: it claims due deliveries from the queue, has each partner's
adapter render them, POSTs them to the partner's endpoint and records what happened (see
``app/webhooks/sender.py`` for the guarantees). Unlike the telemetry worker it can run in
several copies: claiming uses row locks and a lease, so two senders never take the same
delivery at once.

It refuses to start without ``WEBHOOK_ENDPOINTS``. A sender with nowhere to send marks every
delivery ``skipped``, and skipped is final: a typo in a variable name must not quietly discard
a day of notifications. A partner with *no entry* among others is skipped on purpose: it has
not asked to be told.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from datetime import timedelta

import structlog

from app.adapters.registry import AdapterRegistry, default_registry
from app.config import Settings, get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.db.uow import SqlAlchemyUnitOfWork
from app.logging import configure_logging
from app.services.ports import UnitOfWorkFactory
from app.webhooks.http import HttpxPoster, Poster
from app.webhooks.retry import RetryPolicy
from app.webhooks.sender import SenderConfig, WebhookSender

logger = structlog.get_logger(__name__)

EXIT_MISCONFIGURED = 2


class WebhookConfigError(ValueError):
    """The sender cannot run with the configuration it was given."""


def retry_policy_from(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.webhook_max_attempts,
        base_s=settings.webhook_backoff_base_seconds,
        factor=settings.webhook_backoff_factor,
        cap_s=settings.webhook_backoff_cap_seconds,
        jitter=settings.webhook_backoff_jitter,
    )


def sender_config_from(settings: Settings) -> SenderConfig:
    return SenderConfig(
        batch_size=settings.webhook_batch_size,
        lease=timedelta(seconds=settings.webhook_lease_seconds),
        concurrency=settings.webhook_concurrency,
        poll_s=settings.webhook_poll_seconds,
    )


def check_endpoints(settings: Settings, registry: AdapterRegistry) -> None:
    """Refuse a configuration that would quietly drop notifications."""
    if not settings.webhook_endpoints:
        raise WebhookConfigError(
            "no webhook endpoints are configured (set WEBHOOK_ENDPOINTS). A sender with nowhere "
            "to send would mark every delivery skipped, and skipped is final"
        )
    unknown = sorted(set(settings.webhook_endpoints) - set(registry.client_ids()))
    if unknown:
        raise WebhookConfigError(
            f"WEBHOOK_ENDPOINTS names clients that have no adapter: {', '.join(unknown)} "
            f"(known: {', '.join(registry.client_ids())})"
        )


def build_sender(
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    poster: Poster,
    *,
    registry: AdapterRegistry | None = None,
) -> WebhookSender:
    registry = registry or default_registry()
    check_endpoints(settings, registry)
    return WebhookSender(
        uow_factory,
        registry=registry,
        endpoints=settings.webhook_endpoints,
        poster=poster,
        policy=retry_policy_from(settings),
        config=sender_config_from(settings),
    )


async def run_sender(
    settings: Settings,
    stop: asyncio.Event,
    *,
    uow_factory: UnitOfWorkFactory | None = None,
    poster: Poster | None = None,
) -> None:
    """Send until ``stop`` is set. The factory and poster are replaceable for tests."""
    engine = None
    own_poster = None
    if uow_factory is None:
        engine = create_engine_from_settings(settings)
        session_factory = create_session_factory(engine)

        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(session_factory)

        uow_factory = factory
    if poster is None:
        own_poster = HttpxPoster(
            timeout_s=settings.webhook_timeout_seconds,
            connect_timeout_s=settings.webhook_connect_timeout_seconds,
        )
        poster = own_poster
    try:
        sender = build_sender(settings, uow_factory, poster)
        task = asyncio.create_task(sender.run())
        logger.info(
            "webhook_sender_started",
            clients=sorted(settings.webhook_endpoints),  # never the URLs or secrets
            max_attempts=settings.webhook_max_attempts,
        )
        try:
            await stop.wait()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("webhook_sender_stopped", **dict(sender.counts))
    finally:
        if own_poster is not None:
            await own_poster.aclose()
        if engine is not None:
            await engine.dispose()


def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    async def serve() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Not available on Windows: there Ctrl-C raises KeyboardInterrupt, handled below.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run_sender(settings, stop)

    try:
        asyncio.run(serve())
    except WebhookConfigError as problem:
        logger.error("webhook_sender_misconfigured", reason=str(problem))
        print(f"webhook sender not started: {problem}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
