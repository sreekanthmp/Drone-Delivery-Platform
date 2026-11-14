"""Run a job every so often, forever, surviving the job's own failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


async def every(interval_s: float, job: Callable[[], Awaitable[object]], *, name: str) -> None:
    """Call ``job`` every ``interval_s`` seconds until cancelled.

    An exception in one run is logged and the loop carries on: a watchdog that dies on its
    first Redis hiccup is worse than no watchdog.
    """
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("periodic_job_failed", job=name)
        await asyncio.sleep(interval_s)
