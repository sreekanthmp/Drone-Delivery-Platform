"""Run simulated drones::

    python -m app.simulator --api-url http://localhost:8000 --fleet 3 --speed-factor 30

Registers ``--fleet`` drones with the platform (or use ``--drone-id`` for ones that already
exist), connects to the broker, and flies whatever missions the platform sends them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from uuid import UUID

import structlog

from app.config import get_settings
from app.domain.models import Location
from app.logging import configure_logging
from app.mqtt.client import MqttClient
from app.simulator.drone import SimulatedDrone
from app.simulator.registration import register_fleet
from app.simulator.runner import SimulatorRunner

logger = structlog.get_logger(__name__)


def _location(text: str) -> Location:
    latitude, longitude = (float(part) for part in text.split(","))
    return Location(latitude=latitude, longitude=longitude)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.simulator", description=__doc__)
    parser.add_argument("--mqtt-url", default=None, help="default: MQTT_URL from the settings")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--fleet", type=int, default=0, help="register this many drones (sim-1..)")
    parser.add_argument(
        "--drone-id", action="append", default=[], type=UUID, help="an existing drone"
    )
    parser.add_argument("--home", type=_location, default=Location(latitude=12.98, longitude=77.60))
    parser.add_argument("--speed-factor", type=float, default=1.0, help="time acceleration")
    parser.add_argument(
        "--hold-seconds", type=float, default=5.0, help="simulated seconds at stops"
    )
    parser.add_argument("--tick-seconds", type=float, default=0.1)
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    settings = get_settings()
    drones: list[SimulatedDrone] = []
    if args.fleet:
        for drone_id, home in await register_fleet(args.api_url, args.fleet, args.home):
            drones.append(_drone(drone_id, home, args))
    for drone_id in args.drone_id:
        drones.append(_drone(drone_id, args.home, args))
    if not drones:
        raise SystemExit("nothing to simulate: pass --fleet N and/or --drone-id UUID")

    client = MqttClient(
        args.mqtt_url or settings.mqtt_url, client_id=f"sim-{drones[0].drone_id.hex[:12]}"
    )
    runner = SimulatorRunner(drones, client, tick_s=args.tick_seconds)
    task = asyncio.create_task(runner.run())
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    logger.info("simulator_started", drones=[str(d.drone_id) for d in drones])
    await stop.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _drone(drone_id: UUID, home: Location, args: argparse.Namespace) -> SimulatedDrone:
    return SimulatedDrone(
        drone_id, home=home, speed_factor=args.speed_factor, hold_s=args.hold_seconds
    )


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(serve(parse_args()))


if __name__ == "__main__":
    main()
