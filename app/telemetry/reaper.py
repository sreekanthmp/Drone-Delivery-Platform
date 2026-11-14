"""The reaper: releases reservations that a dispatch abandoned, and tells operations.

Runs ``DispatchService.release_stale_reservations`` on a timer. It only ever cancels flights
that never took off (see that method), through the service, audited; it sends nothing to a
drone. Each release is announced as a one-off alert so a person can look into why a dispatch
died.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from app.dispatch.service import DispatchService, StaleRelease
from app.realtime.models import Alert, AlertKind
from app.realtime.ports import AlertBoard

logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Reaper:
    def __init__(
        self,
        dispatch: DispatchService,
        alerts: AlertBoard,
        *,
        older_than: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._dispatch = dispatch
        self._alerts = alerts
        self._older_than = older_than
        self._clock = clock

    async def sweep(self) -> list[StaleRelease]:
        released = await self._dispatch.release_stale_reservations(self._older_than)
        for release in released:
            try:
                await self._alerts.notify(
                    Alert(
                        kind=AlertKind.STALE_RESERVATION,
                        drone_id=release.drone_id,
                        flight_id=release.flight_id,
                        order_id=release.order_id,
                        message=(
                            f"a dispatch left flight {release.flight_id} in {release.was.value} "
                            f"for over {self._older_than}; it was cancelled and the drone freed"
                        ),
                        raised_at=self._clock(),
                    )
                )
            except Exception as error:  # the release itself is done and audited
                logger.warning("reaper_alert_failed", error=repr(error))
        return released
