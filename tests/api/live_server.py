"""Run the real app on a real socket inside the test's event loop, and talk to it.

WebSocket streams cannot be exercised through ``httpx.ASGITransport``, so these tests start the
app under uvicorn (in the same event loop, so the hub and the services share it) and connect
with a real WebSocket client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosed


@contextlib.asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    """Yield ``host:port`` of the app, running until the block ends."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def connect(host: str, path: str) -> Any:
    return websockets.connect(f"ws://{host}{path}", open_timeout=5)


async def frame(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    """The next JSON frame (raises ``TimeoutError`` or ``ConnectionClosed``)."""
    async with asyncio.timeout(timeout):
        body: dict[str, Any] = json.loads(await ws.recv())
        return body


async def frames_until(
    ws: Any, done: Callable[[list[dict[str, Any]]], bool], timeout: float = 5.0
) -> list[dict[str, Any]]:
    """Collect frames (ignoring pings) until ``done(frames)``."""
    seen: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while not done(seen):
            received = await frame(ws, timeout)
            if received.get("type") != "ping":
                seen.append(received)
    return seen


async def close_code(ws: Any, timeout: float = 5.0) -> int:
    """Read until the server closes the connection; return its close code."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                await ws.recv()
    except ConnectionClosed as closed:
        if closed.rcvd is None:
            raise AssertionError("the connection ended without a close frame") from closed
        return int(closed.rcvd.code)


async def quiet(ws: Any, seconds: float = 0.4) -> list[dict[str, Any]]:
    """Everything that arrives in ``seconds`` apart from pings."""
    seen: list[dict[str, Any]] = []
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            while True:
                received = json.loads(await ws.recv())
                if received.get("type") != "ping":
                    seen.append(received)
    return seen
