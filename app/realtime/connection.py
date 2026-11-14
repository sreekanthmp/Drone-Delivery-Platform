"""Connect to Redis. The one place the platform creates a Redis client."""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis


def connect(url: str) -> Any:
    """A client that connects lazily and gives up on a dead server quickly.

    Short timeouts matter: the live view is optional, so a hung Redis must fail fast rather
    than stall the work it merely observes.
    """
    return aioredis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=5.0,
        health_check_interval=30,
    )
