"""The loop every stream runs: send what the hub delivers, ping when quiet, stop cleanly.

Read-only by construction: the only thing it looks at from the client is whether it has
disconnected. Anything a client sends is discarded.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import WebSocket

from app.realtime.hub import Subscription, SubscriptionInterrupted
from app.realtime.models import Envelope

logger = structlog.get_logger(__name__)

CLOSE_NORMAL = 1000
CLOSE_TRY_AGAIN = 1013
"""The stream cannot be served right now (no live feed, or the watcher must re-sync)."""

CLOSE_NOT_FOUND = 4404
"""Application code: the order (or the client) in the URL does not exist."""


@dataclass(frozen=True, slots=True)
class Out:
    """What one live message turns into: frames to send, and whether the stream is done."""

    frames: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    finished: bool = False


async def refuse(
    websocket: WebSocket,
    code: int,
    reason: str,
    frame: Mapping[str, Any] | None = None,
) -> None:
    """Accept just to say no, in a way the client can read: an error frame, then a close code."""
    await websocket.accept()
    if frame is not None:
        with contextlib.suppress(Exception):
            await websocket.send_json(frame)
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


async def _until_disconnect(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # Anything else the client sends is ignored: this stream is read-only.
    except Exception:
        return


async def run_stream(
    websocket: WebSocket,
    subscription: Subscription,
    *,
    ping_s: float,
    on_message: Callable[[str, Envelope], Awaitable[Out]],
) -> None:
    """Serve the stream until the client leaves, the watcher must re-sync, or it finishes."""
    gone = asyncio.create_task(_until_disconnect(websocket))
    try:
        while True:
            waiting = asyncio.create_task(subscription.get(ping_s))
            done, _ = await asyncio.wait({waiting, gone}, return_when=asyncio.FIRST_COMPLETED)
            if gone in done:
                waiting.cancel()
                return
            try:
                message = waiting.result()
            except SubscriptionInterrupted as interruption:
                logger.info("stream_interrupted", reason=str(interruption))
                await websocket.close(code=CLOSE_TRY_AGAIN, reason="re-sync: reconnect")
                return
            if message is None:
                await websocket.send_json({"type": "ping"})
                continue
            out = await on_message(*message)
            for frame in out.frames:
                await websocket.send_json(frame)
            if out.finished:
                await websocket.close(code=CLOSE_NORMAL, reason="finished")
                return
    except Exception as error:  # a client that vanished mid-send, or a dying connection
        logger.debug("stream_ended", error=repr(error))
    finally:
        gone.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gone
        subscription.close()
