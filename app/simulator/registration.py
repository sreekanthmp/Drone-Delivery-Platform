"""Register simulated drones with the platform's internal API (so they exist in its fleet)."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID

import structlog

from app.domain.models import Location

logger = structlog.get_logger(__name__)


def _call(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


async def register_fleet(api_url: str, count: int, home: Location) -> list[tuple[UUID, Location]]:
    """Register ``sim-1`` .. ``sim-N`` (reusing any that already exist). Waits for the API."""
    base = api_url.rstrip("/")
    for attempt in range(60):
        try:
            status, _ = await asyncio.to_thread(_call, "GET", f"{base}/health")
            if status == 200:
                break
        except OSError:
            pass
        logger.info("waiting_for_api", url=base, attempt=attempt)
        await asyncio.sleep(1.0)
    else:
        raise RuntimeError(f"the platform API at {base} did not become reachable")

    fleet: list[tuple[UUID, Location]] = []
    for number in range(1, count + 1):
        name = f"sim-{number}"
        body = {
            "name": name,
            "home": {"latitude": home.latitude, "longitude": home.longitude},
            "max_payload_kg": 5.0,
            "max_range_km": 20.0,
            "battery_percent": 100.0,
        }
        status, drone = await asyncio.to_thread(_call, "POST", f"{base}/internal/drones", body)
        if status == 409:  # already registered by an earlier run
            _, listing = await asyncio.to_thread(_call, "GET", f"{base}/internal/drones")
            drone = next(d for d in listing if d["name"] == name)
        elif status != 201:
            raise RuntimeError(f"could not register {name}: HTTP {status} {drone}")
        fleet.append(
            (
                UUID(drone["id"]),
                Location(latitude=drone["home"]["latitude"], longitude=drone["home"]["longitude"]),
            )
        )
        logger.info("drone_ready", name=name, drone_id=drone["id"])
    return fleet
