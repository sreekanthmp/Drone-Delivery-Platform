"""The HTTP poster and the sender against a real HTTP server on a real socket.

What a mock transport cannot prove: that timeouts really fire, that a refused connection and a
silent partner are survivable, that a redirect is not followed, and that a signed request
arrives intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.domain.models import DeliveryStatus
from app.services.memory import InMemoryDatabase, InMemoryUnitOfWork
from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.http import HttpxPoster
from app.webhooks.retry import RetryPolicy
from app.webhooks.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from tests.api.live_server import serve
from tests.webhook_rig import build_webhook_rig


def partner_app(received: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/ok")
    async def ok(request: Request) -> Response:
        received.append(
            {"body": await request.body(), "headers": dict(request.headers), "path": "/ok"}
        )
        return Response(status_code=204)

    @app.post("/busy")
    async def busy() -> Response:
        return Response(status_code=503, headers={"Retry-After": "7"})

    @app.post("/slow")
    async def slow() -> Response:
        await asyncio.sleep(5)
        return Response(status_code=200)

    @app.post("/redirect")
    async def redirect() -> Response:
        return Response(status_code=307, headers={"Location": "/ok"})

    @app.post("/rejected")
    async def rejected() -> JSONResponse:
        return JSONResponse({"error": "no such order"}, status_code=404)

    return app


@pytest.fixture
async def partner() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []
    async with serve(partner_app(received)) as host:
        yield f"http://{host}", received


@pytest.fixture
async def poster() -> AsyncIterator[HttpxPoster]:
    instance = HttpxPoster(timeout_s=1.0, connect_timeout_s=0.5)
    try:
        yield instance
    finally:
        await instance.aclose()


@contextlib.asynccontextmanager
async def raw_partner(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[int]:
    """A bare TCP server running ``handler``; every connection is closed when the block ends.

    (On Python 3.12 ``Server.wait_closed`` waits for open connections, so a fake partner that
    never hangs up would otherwise hold the test, not the poster.)
    """
    writers: list[asyncio.StreamWriter] = []

    async def wrapped(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        with contextlib.suppress(Exception):
            await handler(reader, writer)

    server = await asyncio.start_server(wrapped, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        for writer in writers:
            writer.close()
        async with asyncio.timeout(5):
            await server.wait_closed()


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestARealPartner:
    async def test_a_delivery_arrives_intact(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, received = partner

        result = await poster.post(
            f"{base}/ok", body=b'{"status":"created"}', headers={"X-Event-Id": "abc"}
        )

        assert (result.status_code, result.error) == (204, None)
        (got,) = received
        assert got["body"] == b'{"status":"created"}'
        assert got["headers"]["x-event-id"] == "abc"

    async def test_a_busy_partner_and_its_retry_after(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, _ = partner

        result = await poster.post(f"{base}/busy", body=b"{}", headers={})

        assert (result.status_code, result.retry_after_s) == (503, 7.0)

    async def test_a_rejection_is_reported_with_its_status_only(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, _ = partner

        result = await poster.post(f"{base}/rejected", body=b"{}", headers={})

        assert (result.status_code, result.error) == (404, None)

    async def test_a_redirect_is_not_followed(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, received = partner

        result = await poster.post(f"{base}/redirect", body=b"{}", headers={})

        assert result.status_code == 307
        assert received == []  # /ok was never requested

    async def test_a_slow_partner_times_out_at_the_deadline(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, _ = partner
        started = time.monotonic()

        result = await poster.post(f"{base}/slow", body=b"{}", headers={})

        assert (result.status_code, result.error) == (None, "timeout")
        assert 0.8 <= time.monotonic() - started < 3.0  # the 1 s limit, not the server's 5 s

    async def test_nothing_listening_is_a_connection_failure(self, poster: HttpxPoster) -> None:
        result = await poster.post(f"http://127.0.0.1:{unused_port()}/hook", body=b"{}", headers={})

        assert (result.status_code, result.error) == (None, "connection failed")

    async def test_a_partner_that_accepts_and_never_answers_cannot_hold_the_sender(
        self, poster: HttpxPoster
    ) -> None:
        async def say_nothing(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()  # swallow the request, never reply

        async with raw_partner(say_nothing) as port:
            started = time.monotonic()
            result = await poster.post(f"http://127.0.0.1:{port}/hook", body=b"{}", headers={})

        assert (result.status_code, result.error) == (None, "timeout")
        assert time.monotonic() - started < 3.0

    async def test_a_partner_that_drips_a_response_forever_hits_the_total_deadline(
        self, poster: HttpxPoster
    ) -> None:
        """Per-step timeouts alone would never fire: a byte every 0.3 s keeps each step alive."""

        async def drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nX-Slow:")
            for _ in range(100):
                writer.write(b" x")
                await writer.drain()
                await asyncio.sleep(0.3)

        async with raw_partner(drip) as port:
            started = time.monotonic()
            result = await poster.post(f"http://127.0.0.1:{port}/hook", body=b"{}", headers={})

        assert (result.status_code, result.error) == (None, "timeout")
        assert time.monotonic() - started < 3.0


class TestTheWholeSenderOverRealHttp:
    async def test_a_signed_update_reaches_the_partner_and_is_marked_delivered(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, received = partner
        database = InMemoryDatabase()
        rig = build_webhook_rig(
            lambda: InMemoryUnitOfWork(database),
            endpoints={
                "mock": WebhookEndpoint(url=f"{base}/ok", secret="s3cret")  # type: ignore[arg-type]
            },
            poster=poster,  # type: ignore[arg-type]
        )
        order = await rig.new_order("ORD-9")

        await rig.drain()

        (got,) = received
        headers = got["headers"]
        assert verify(
            "s3cret",
            int(headers[TIMESTAMP_HEADER.lower()]),
            got["body"],
            headers[SIGNATURE_HEADER.lower()],
        )
        assert b'"order_id":"ORD-9"' in got["body"]
        (delivery,) = await rig.deliveries(order)
        assert delivery.status is DeliveryStatus.DELIVERED

    async def test_a_partner_that_is_down_is_retried_then_recovers(
        self, partner: tuple[str, list[dict[str, Any]]], poster: HttpxPoster
    ) -> None:
        base, received = partner
        database = InMemoryDatabase()
        endpoint = WebhookEndpoint(url=f"http://127.0.0.1:{unused_port()}/ok")
        rig = build_webhook_rig(
            lambda: InMemoryUnitOfWork(database),
            endpoints={"mock": endpoint},
            poster=poster,  # type: ignore[arg-type]
            policy=RetryPolicy(base_s=1, factor=1, jitter=0.0),
        )
        order = await rig.new_order()
        await rig.drain()  # nothing is listening
        (waiting,) = await rig.deliveries(order)
        assert waiting.status is DeliveryStatus.PENDING
        assert waiting.last_error == "connection failed"

        rig.sender._endpoints["mock"] = WebhookEndpoint(url=f"{base}/ok")  # the partner is back
        rig.clock.advance(seconds=1)
        await rig.drain()

        (delivery,) = await rig.deliveries(order)
        assert (delivery.status, delivery.attempts) == (DeliveryStatus.DELIVERED, 2)
        assert len(received) == 1
