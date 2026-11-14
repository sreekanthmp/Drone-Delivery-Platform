"""The one place a webhook leaves the process: an HTTP POST with hard limits.

``Poster.post`` never raises for anything the network or the partner can do; it reports it as a
:class:`~app.webhooks.retry.SendResult` and the retry policy decides what it means. Limits:

* a total deadline for the whole request (connecting, sending, waiting for the status line),
  not just per-step timeouts, so a partner that drips bytes cannot hold a sender forever;
* redirects are never followed (a redirect is a permanent failure, and following one would let a
  partner send the platform to a host nobody configured);
* the response body is never read: only the status and ``Retry-After`` matter.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from app.webhooks.retry import SendResult


class Poster(Protocol):
    async def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> SendResult: ...


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date), else ``None``."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, seconds)


class HttpxPoster:
    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        connect_timeout_s: float = 3.0,
        client: httpx.AsyncClient | None = None,
        close_client: bool | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._timeout = httpx.Timeout(timeout_s, connect=min(connect_timeout_s, timeout_s))
        # A client we were handed is the caller's to close, unless they say otherwise.
        self._owns_client = (client is None) if close_client is None else close_client
        self._client = client or httpx.AsyncClient()

    @classmethod
    def in_process(cls, app: Any, *, timeout_s: float = 10.0) -> HttpxPoster:
        """A poster that talks to an ASGI app in this process, with no socket (demo and tests).

        It is the same code path as a real POST: the same limits, the same result mapping.
        """
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        return cls(timeout_s=timeout_s, client=client, close_client=True)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> SendResult:
        started = time.monotonic()

        def took() -> int:
            return round((time.monotonic() - started) * 1000)

        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._client.stream(
                    "POST",
                    url,
                    content=body,
                    headers=dict(headers),
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    return SendResult(
                        status_code=response.status_code,
                        duration_ms=took(),
                        retry_after_s=parse_retry_after(response.headers.get("retry-after")),
                    )
        except (TimeoutError, httpx.TimeoutException):
            return SendResult(status_code=None, duration_ms=took(), error="timeout")
        except httpx.ConnectError:
            return SendResult(status_code=None, duration_ms=took(), error="connection failed")
        except httpx.HTTPError as error:
            return SendResult(
                status_code=None,
                duration_ms=took(),
                error=f"request failed ({type(error).__name__})",
            )
