"""The HTTP poster, over a mock transport: how responses and failures become results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.webhooks.http import HttpxPoster, parse_retry_after

URL = "https://partner.example/hook"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class TestRetryAfter:
    @pytest.mark.parametrize(("header", "seconds"), [("120", 120.0), (" 7 ", 7.0), ("0", 0.0)])
    def test_delta_seconds(self, header: str, seconds: float) -> None:
        assert parse_retry_after(header, now=NOW) == seconds

    def test_an_http_date(self) -> None:
        header = format_datetime(NOW + timedelta(seconds=90), usegmt=True)

        assert parse_retry_after(header, now=NOW) == pytest.approx(90)

    def test_a_date_in_the_past_is_zero(self) -> None:
        header = format_datetime(NOW - timedelta(hours=1), usegmt=True)

        assert parse_retry_after(header, now=NOW) == 0.0

    @pytest.mark.parametrize("header", [None, "", "  ", "soon", "-5", "1.5", "12abc", "١٢٣"])
    def test_anything_else_is_ignored(self, header: str | None) -> None:
        assert parse_retry_after(header, now=NOW) is None


def poster_over(handler: httpx.MockTransport) -> HttpxPoster:
    return HttpxPoster(client=httpx.AsyncClient(transport=handler), timeout_s=2.0)


class TestPosting:
    async def test_it_sends_the_body_and_headers_as_given(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(204)

        result = await poster_over(httpx.MockTransport(handler)).post(
            URL, body=b'{"a":1}', headers={"X-Event-Id": "e-1", "Content-Type": "application/json"}
        )

        (request,) = seen
        assert (request.method, str(request.url)) == ("POST", URL)
        assert request.content == b'{"a":1}'
        assert request.headers["X-Event-Id"] == "e-1"
        assert (result.status_code, result.error, result.retry_after_s) == (204, None, None)
        assert result.duration_ms >= 0

    @pytest.mark.parametrize("code", [200, 400, 404, 429, 500, 503])
    async def test_any_status_is_reported_not_raised(self, code: int) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(code))

        result = await poster_over(transport).post(URL, body=b"{}", headers={})

        assert (result.status_code, result.error) == (code, None)

    async def test_a_retry_after_header_is_passed_on(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "30"})
        )

        result = await poster_over(transport).post(URL, body=b"{}", headers={})

        assert result.retry_after_s == 30

    async def test_a_redirect_is_reported_as_it_is_and_not_followed(self) -> None:
        hits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hits.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})

        result = await poster_over(httpx.MockTransport(handler)).post(URL, body=b"{}", headers={})

        assert result.status_code == 302
        assert hits == [URL]

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (httpx.ConnectTimeout("slow"), "timeout"),
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.PoolTimeout("busy"), "timeout"),
            (httpx.ConnectError("refused"), "connection failed"),
            (httpx.ReadError("reset"), "request failed (ReadError)"),
            (httpx.RemoteProtocolError("bad"), "request failed (RemoteProtocolError)"),
            (httpx.UnsupportedProtocol("odd"), "request failed (UnsupportedProtocol)"),
        ],
    )
    async def test_a_network_failure_is_a_result_with_no_status(
        self, raised: Exception, expected: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise raised

        result = await poster_over(httpx.MockTransport(handler)).post(URL, body=b"{}", headers={})

        assert (result.status_code, result.error) == (None, expected)

    async def test_an_error_never_echoes_the_url_or_the_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url} with {request.content!r}")

        result = await poster_over(httpx.MockTransport(handler)).post(
            URL, body=b"secret-body", headers={}
        )

        assert result.error == "connection failed"

    async def test_the_response_body_is_never_read(self) -> None:
        class Endless(httpx.AsyncByteStream):
            read = False

            async def __aiter__(self):  # type: ignore[no-untyped-def]
                Endless.read = True
                yield b"x" * 10

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Endless()))

        result = await poster_over(transport).post(URL, body=b"{}", headers={})

        assert result.status_code == 200
        assert Endless.read is False
