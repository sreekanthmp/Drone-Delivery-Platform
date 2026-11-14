"""The watchdog: notices what is wrong with drones in the air, and says so. Alerts only.

Each pass compares the platform's live flights with the drones' own latest samples:

* **link lost**: a flight that is started or paused, but no sample for a while;
* **low battery**: a drone in the air below the threshold;
* **state mismatch**: the platform thinks a drone is flying but it reports idle or landed (a
  lost ``mission_completed``, a failsafe landing), or the drone reports flying with no live
  flight on the platform's side. It must persist for a grace period: events and samples
  legitimately disagree for a moment.

It **never sends a command and never changes a status**. Deciding that a drone should be
recalled is a safety policy for a person; this only makes sure a person knows. It holds no
link and no stores, only the read side of the dispatch service and the live view.
Alerts are standing: raised once, cleared (and announced as resolved) when the cause is gone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.dispatch.service import DispatchService
from app.domain.models import Flight, FlightStatus
from app.protocol.messages import DronePhase
from app.realtime.models import Alert, AlertKind, DroneState
from app.realtime.ports import AlertBoard, StateStore

logger = structlog.get_logger(__name__)

AIRBORNE = {FlightStatus.STARTED, FlightStatus.PAUSED}
ON_THE_GROUND = {DronePhase.IDLE, DronePhase.READY, DronePhase.LANDED}
MANAGED = {AlertKind.LINK_LOST, AlertKind.LOW_BATTERY, AlertKind.STATE_MISMATCH}


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    link_lost_after: timedelta = timedelta(seconds=15)
    low_battery_percent: float = 20.0
    mismatch_grace: timedelta = timedelta(seconds=30)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Watchdog:
    def __init__(
        self,
        dispatch: DispatchService,
        state: StateStore,
        alerts: AlertBoard,
        *,
        config: WatchdogConfig | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._dispatch = dispatch
        self._state = state
        self._alerts = alerts
        self._config = config or WatchdogConfig()
        self._clock = clock
        self._mismatch_since: dict[UUID, datetime] = {}

    async def check(self) -> list[Alert]:
        """One pass. Returns the alerts newly raised (clearing is announced on the board)."""
        now = self._clock()
        states = await self._state.all_states()
        live_flights = await self._dispatch.list_live_flights()
        wanted = self._wanted(now, states, live_flights)

        raised: list[Alert] = []
        for alert in wanted.values():
            if await self._alerts.raise_alert(alert):
                logger.warning(
                    "alert_raised",
                    kind=alert.kind.value,
                    drone_id=str(alert.drone_id),
                    detail=alert.message,
                )
                raised.append(alert)
        for active in await self._alerts.active():
            cleared = active.kind in MANAGED and active.key not in wanted
            if cleared and await self._alerts.resolve(active.kind, active.drone_id, at=now):
                logger.info("alert_resolved", kind=active.kind.value, drone_id=str(active.drone_id))
        return raised

    def _wanted(
        self, now: datetime, states: dict[UUID, DroneState], live_flights: list[Flight]
    ) -> dict[str, Alert]:
        wanted: dict[str, Alert] = {}

        def want(alert: Alert) -> None:
            wanted[alert.key] = alert

        airborne = [f for f in live_flights if f.status in AIRBORNE]
        for flight in airborne:
            state = states.get(flight.drone_id)
            # With no sample on record, the last news of this flight is when it last changed.
            last_heard = state.received_at if state else flight.updated_at
            silent_for = now - last_heard
            if silent_for >= self._config.link_lost_after:
                want(
                    self._alert(
                        AlertKind.LINK_LOST,
                        flight,
                        now,
                        f"no telemetry for {int(silent_for.total_seconds())} s while the flight "
                        f"is {flight.status.value}",
                    )
                )
            if state is not None and state.battery_percent < self._config.low_battery_percent:
                want(
                    self._alert(
                        AlertKind.LOW_BATTERY,
                        flight,
                        now,
                        f"battery at {state.battery_percent:.0f}% (below "
                        f"{self._config.low_battery_percent:.0f}%) while the flight is "
                        f"{flight.status.value}",
                    )
                )

        mismatched = self._mismatches(states, airborne)
        for drone_id in list(self._mismatch_since):
            if drone_id not in mismatched:
                del self._mismatch_since[drone_id]  # the disagreement went away: restart the clock
        for drone_id, (disputed, why) in mismatched.items():
            since = self._mismatch_since.setdefault(drone_id, now)
            if now - since >= self._config.mismatch_grace:
                want(self._alert(AlertKind.STATE_MISMATCH, disputed, now, why, drone_id=drone_id))
        return wanted

    def _mismatches(
        self, states: dict[UUID, DroneState], airborne: list[Flight]
    ) -> dict[UUID, tuple[Flight | None, str]]:
        found: dict[UUID, tuple[Flight | None, str]] = {}
        flying = {f.drone_id: f for f in airborne}
        for drone_id, flight in flying.items():
            state = states.get(drone_id)
            if state is not None and state.phase in ON_THE_GROUND:
                found[drone_id] = (
                    flight,
                    f"the platform has the flight {flight.status.value}, "
                    f"but the drone reports {state.phase.value}",
                )
        for drone_id, state in states.items():
            if drone_id not in flying and state.phase in {DronePhase.FLYING, DronePhase.PAUSED}:
                found[drone_id] = (
                    None,
                    f"the drone reports {state.phase.value}, "
                    "but the platform has no live flight for it",
                )
        return found

    @staticmethod
    def _alert(
        kind: AlertKind,
        flight: Flight | None,
        now: datetime,
        message: str,
        *,
        drone_id: UUID | None = None,
    ) -> Alert:
        return Alert(
            kind=kind,
            drone_id=flight.drone_id if flight is not None else drone_id,
            flight_id=None if flight is None else flight.id,
            order_id=None if flight is None else flight.order_id,
            message=message,
            raised_at=now,
        )
