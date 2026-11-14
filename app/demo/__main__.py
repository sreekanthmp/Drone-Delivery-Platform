"""``python -m app.demo``: the platform, in one process, with a map to watch it on."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.demo.demo import PARTNER_PATH, Demo, build_demo, demo_config
from app.logging import configure_logging

PAGE = Path(__file__).with_name("index.html")


def add_demo_routes(demo: Demo) -> None:
    """The page, its map data, the airspace switch, and the demo partner's server."""
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

    # The demo plays a partner too, so the platform's webhook sender has someone to tell. This
    # is the partner's server, not part of the platform: the platform only ever POSTs to it.
    @router.post(PARTNER_PATH, include_in_schema=False)
    async def partner_webhook(request: Request) -> Response:
        code = demo.partner.handle(body=await request.body(), headers=request.headers)
        return Response(status_code=code)

    @router.get("/demo/partner/view", include_in_schema=False)
    async def partner_view() -> dict[str, Any]:
        return await demo.partner_view()

    @router.post("/demo/partner/down", include_in_schema=False)
    async def partner_down(down: bool) -> dict[str, bool]:
        demo.partner.down = down
        return {"down": down}

    demo.app.include_router(router)


async def serve(host: str, port: int, speed: float) -> None:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    demo = await build_demo(speed=speed, webhook_url=f"http://{shown}:{port}{PARTNER_PATH}")
    add_demo_routes(demo)
    server = uvicorn.Server(uvicorn.Config(demo.app, host=host, port=port, log_level="warning"))
    tasks = [asyncio.create_task(demo.run_world()), asyncio.create_task(demo.run_webhooks())]
    print(
        f"\n  Drone delivery demo is running.\n  Open  http://{host}:{port}\n  (Ctrl-C to stop)\n"
    )
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await demo.poster.aclose()


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
