"""Internal alerts: what the watchdog, the ingest and the reaper have flagged.

Read-only, like everything that reports on the live view. An alert is information for a
person; nothing in the platform acts on one.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.errors import error_response
from app.api.internal.schemas import AlertsOut

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal/alerts", tags=["internal"])


@router.get("", response_model=AlertsOut)
async def get_alerts(
    request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> AlertsOut | JSONResponse:
    """Standing alerts and the latest raised or resolved. Empty (``live: false``) without Redis."""
    live = request.app.state.realtime
    if live is None:
        return AlertsOut(live=False, active=[], recent=[])
    try:
        active = await live.active()
        recent = await live.recent(limit)
    except Exception:
        logger.exception("alerts_unavailable")
        return error_response(503, "live_view_unavailable", "the live view is unavailable")
    return AlertsOut.of(active, recent)
