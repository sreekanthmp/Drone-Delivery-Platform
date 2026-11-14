"""``python -m app.demo``: the platform, in one process, with a map to watch it on."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.demo.demo import Demo, build_demo, demo_config
from app.logging import configure_logging

PAGE = Path(__file__).with_name("index.html")


def add_demo_routes(demo: Demo) -> None:
    """The page itself, its map data, and the one demo-only switch (close the airspace)."""
    router = APIRouter(tags=["demo"])

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @router.get("/demo/config", include_in_schema=False)
    async def config() -> dict[str, Any]:
        return demo_config(demo)

    @router.post("/demo/airspace", include_in_schema=False)
    async def airspace(closed: bool) -> dict[str, bool]:
        demo.set_airspace_closed(closed)
        return {"closed": closed}

    demo.app.include_router(router)


async def serve(host: str, port: int, speed: float) -> None:
    demo = await build_demo(speed=speed)
    add_demo_routes(demo)
    server = uvicorn.Server(uvicorn.Config(demo.app, host=host, port=port, log_level="warning"))
    world = asyncio.create_task(demo.run_world())
    print(
        f"\n  Drone delivery demo is running.\n  Open  http://{host}:{port}\n  (Ctrl-C to stop)\n"
    )
    try:
        await server.serve()
    finally:
        world.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await world


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.demo", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--speed", type=float, default=60.0, help="drone clock fast-forward")
    args = parser.parse_args()
    configure_logging(level="WARNING", json_output=False)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.host, args.port, args.speed))


if __name__ == "__main__":
    main()
