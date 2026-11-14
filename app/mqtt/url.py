"""Parsing of the broker URL (kept apart from the client so settings can validate it)."""

from __future__ import annotations

from urllib.parse import urlsplit


def parse_mqtt_url(url: str) -> tuple[str, int]:
    """``mqtt://host[:port]`` -> ``(host, port)``. No TLS or credentials: none are used."""
    parts = urlsplit(url)
    if parts.scheme != "mqtt" or not parts.hostname:
        raise ValueError(f"expected mqtt://host[:port], got {url!r}")
    return parts.hostname, parts.port or 1883
