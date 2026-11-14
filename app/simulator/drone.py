"""One simulated drone: the drone side of the protocol, as plain logic driven by a clock.

No network and no sleeping here: ``on_command`` answers a command and ``advance`` moves time
forward and returns the events that happened, so the whole behaviour is testable in
microseconds. The runner (``runner.py``) puts it on MQTT.

Like a real drone it does not trust the cloud blindly: it recomputes the mission checksum
from what it received, refuses a start whose authorisation is for another drone, expired, or
for a different mission than the one on board, and refuses to start when it is already
flying. It flies in straight lines at the mission's cruise speed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.exceptions import MissionValidationError
from app.domain.geo import haversine_m
from app.domain.models import Location, Mission, WaypointKind
from app.protocol import messages as wire


class Phase(StrEnum):
    IDLE = "idle"
    READY = "ready"
    """A mission is on board and authorised to start."""

    FLYING = "flying"
    PAUSED = "paused"
    RETURNING = "returning"
    LANDED = "landed"
    """Grounded where it stood (a land command): it stays there until recovered."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _toward(a: Location, b: Location, fraction: float) -> Location:
    """A point ``fraction`` of the way from ``a`` to ``b`` (straight line: fine at this scale)."""
    return Location(
        latitude=a.latitude + (b.latitude - a.latitude) * fraction,
        longitude=a.longitude + (b.longitude - a.longitude) * fraction,
    )


class SimulatedDrone:
    def __init__(
        self,
        drone_id: UUID,
        *,
        home: Location,
        speed_factor: float = 1.0,
        hold_s: float = 5.0,
        approach_radius_m: float = 300.0,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if speed_factor <= 0:
            raise ValueError("speed_factor must be positive")
        self.drone_id = drone_id
        self.home = home
        self.speed_factor = speed_factor
        self.hold_s = hold_s
        self.approach_radius_m = approach_radius_m
        self._clock = clock
        self._ids = id_factory

        self.phase = Phase.IDLE
        self.position = home
        self.mission: Mission | None = None
        self.flight_id: UUID | None = None
        self._seq = 0
        self._wp = 1
        self._hold_left = 0.0
        self._after_hold = ""
        self._arriving_sent = False
        self._events: list[wire.DroneEvent] = []

        # Misbehaviour for tests: act on a command but never acknowledge it, or refuse it.
        self.silent: set[str] = set()
        self.refuse: dict[str, str] = {}

    # ==========================================================================================
    # Commands
    # ==========================================================================================

    def on_command(self, command: wire.AnyCommand) -> wire.AckMessage | None:
        """Handle a command. Returns the acknowledgement, or ``None`` if it stays silent."""
        if command.drone_id != self.drone_id:
            return self._ack(command, "rejected", "the command is for another drone")
        if command.type in self.refuse:
            return self._ack(command, "rejected", self.refuse[command.type])

        match command:
            case wire.UploadMission():
                ack = self._upload(command)
            case wire.StartMission():
                ack = self._start(command)
            case wire.PauseMission():
                ack = self._pause(command)
            case wire.ReturnToHome():
                ack = self._return_home(command)
            case wire.Land():
                ack = self._land(command)
        return None if command.type in self.silent else ack

    def _ack(
        self,
        command: wire.AnyCommand,
        status: str,
        reason: str | None = None,
        *,
        checksum: str | None = None,
    ) -> wire.AckMessage:
        return wire.AckMessage(
            command_id=command.command_id,
            drone_id=self.drone_id,
            status=wire.AckStatus(status),
            acked_at=self._clock(),
            reason=reason,
            mission_checksum=checksum,
        )

    def _upload(self, command: wire.UploadMission) -> wire.AckMessage:
        if self.phase in {Phase.FLYING, Phase.PAUSED, Phase.RETURNING}:
            return self._ack(command, "rejected", "cannot load a mission while airborne")
        try:
            mission = command.mission.to_domain()
        except MissionValidationError as error:
            return self._ack(command, "rejected", f"invalid mission: {error}")
        if mission.checksum != command.mission.checksum:
            return self._ack(command, "rejected", "the mission does not match its checksum")
        self.mission = mission
        self.phase = Phase.READY
        return self._ack(command, "accepted", checksum=mission.checksum)

    def _start(self, command: wire.StartMission) -> wire.AckMessage:
        """Start (from ready) or resume (from paused), but only with a valid authorisation."""
        auth = command.authorization
        if self.phase not in {Phase.READY, Phase.PAUSED} or self.mission is None:
            return self._ack(command, "rejected", f"cannot start while {self.phase.value}")
        if auth.drone_id != self.drone_id:
            return self._ack(command, "rejected", "the authorisation is for another drone")
        if self._clock() >= auth.valid_until:
            return self._ack(command, "rejected", "the authorisation has expired")
        if auth.mission_checksum != self.mission.checksum:
            return self._ack(command, "rejected", "the authorisation is for a different mission")
        if self.phase is Phase.PAUSED:
            if auth.flight_id != self.flight_id:
                return self._ack(command, "rejected", "the authorisation is for another flight")
            self.phase = Phase.FLYING
            return self._ack(command, "accepted")
        self.flight_id = auth.flight_id
        self._seq = 0
        self._wp = 1
        self._hold_left = 0.0
        self._arriving_sent = False
        self.position = self.mission.waypoints[0].location
        self.phase = Phase.FLYING
        return self._ack(command, "accepted")

    def _pause(self, command: wire.PauseMission) -> wire.AckMessage:
        if self.phase is not Phase.FLYING:
            return self._ack(command, "rejected", f"not flying: {self.phase.value}")
        self.phase = Phase.PAUSED
        return self._ack(command, "accepted")

    def _return_home(self, command: wire.ReturnToHome) -> wire.AckMessage:
        if self.phase is Phase.RETURNING:
            return self._ack(command, "accepted")
        if self.phase not in {Phase.FLYING, Phase.PAUSED}:
            return self._ack(command, "rejected", f"not flying: {self.phase.value}")
        self.phase = Phase.RETURNING
        self._hold_left = 0.0
        self._after_hold = ""
        return self._ack(command, "accepted")

    def _land(self, command: wire.Land) -> wire.AckMessage:
        if self.phase not in {Phase.FLYING, Phase.PAUSED, Phase.RETURNING}:
            return self._ack(command, "rejected", f"not flying: {self.phase.value}")
        self.phase = Phase.LANDED
        self._hold_left = 0.0
        return self._ack(command, "accepted")

    # ==========================================================================================
    # Time
    # ==========================================================================================

    def advance(self, real_seconds: float) -> list[wire.DroneEvent]:
        """Let ``real_seconds`` pass (scaled by ``speed_factor``); return what was reported."""
        self._events = []
        budget = real_seconds * self.speed_factor
        while budget > 1e-9 and self.phase in {Phase.FLYING, Phase.RETURNING}:
            if self._hold_left > 0:
                used = min(self._hold_left, budget)
                self._hold_left -= used
                budget -= used
                if self._hold_left <= 1e-9:
                    self._hold_left = 0.0
                    self._finish_hold()
                continue
            budget = self._fly(budget)
        return self._events

    def dock(self) -> list[wire.DroneEvent]:
        """A grounded drone is carried back to its dock (what operations does after a land)."""
        self._events = []
        if self.phase is Phase.LANDED:
            self.position = self.home
            self.phase = Phase.IDLE
            self._emit(wire.EventType.DOCKED)
        return self._events

    def fault(self, code: str, message: str = "") -> list[wire.DroneEvent]:
        """Report a fault (it changes nothing about how the drone flies)."""
        self._events = []
        self._emit(wire.EventType.FAULT, {"code": code, "message": message})
        return self._events

    # --- internals ---------------------------------------------------------------------------

    def _target(self) -> tuple[Location, WaypointKind | None]:
        if self.phase is Phase.RETURNING or self.mission is None:
            return self.home, None
        waypoint = self.mission.waypoints[self._wp]
        return waypoint.location, waypoint.kind

    def _fly(self, budget: float) -> float:
        """Move toward the current target; return the time budget left over."""
        assert self.mission is not None
        target, _ = self._target()
        speed = self.mission.cruise_speed_mps
        distance = haversine_m(self.position, target)
        step = speed * budget
        if distance > 1e-6 and step < distance:
            self.position = _toward(self.position, target, step / distance)
            self._check_approach()
            return 0.0
        self.position = target
        self._arrive()
        return budget - distance / speed

    def _arrive(self) -> None:
        assert self.mission is not None
        if self.phase is Phase.RETURNING:
            self.phase = Phase.IDLE
            self._emit(wire.EventType.DOCKED)
            return
        kind = self.mission.waypoints[self._wp].kind
        if kind is WaypointKind.PICKUP:
            self._emit(wire.EventType.PACKAGE_PICKED_UP)
            self._start_hold("depart")
        elif kind is WaypointKind.DROPOFF:
            self._check_approach(force=True)
            self._start_hold("deliver")
        elif kind is WaypointKind.LAND:
            self._emit(wire.EventType.MISSION_COMPLETED)
            self.phase = Phase.IDLE
            self.mission = None
        else:
            self._wp += 1

    def _start_hold(self, after: str) -> None:
        self._after_hold = after
        self._hold_left = self.hold_s
        if self.hold_s <= 0:
            self._finish_hold()

    def _finish_hold(self) -> None:
        if self._after_hold == "depart":
            self._emit(wire.EventType.DEPARTED_PICKUP)
            self._wp += 1
            self._arriving_sent = False
            self._check_approach()
        elif self._after_hold == "deliver":
            self._emit(wire.EventType.PACKAGE_DELIVERED)
            self._wp += 1
        self._after_hold = ""

    def _check_approach(self, *, force: bool = False) -> None:
        """Report that the drone is closing in on the dropoff (once)."""
        if self.mission is None or self.phase is not Phase.FLYING or self._arriving_sent:
            return
        waypoint = self.mission.waypoints[self._wp]
        if waypoint.kind is not WaypointKind.DROPOFF:
            return
        if force or haversine_m(self.position, waypoint.location) <= self.approach_radius_m:
            self._arriving_sent = True
            self._emit(wire.EventType.ARRIVING_AT_DROPOFF)

    def _emit(self, type_: wire.EventType, detail: dict[str, str] | None = None) -> None:
        assert self.flight_id is not None
        self._seq += 1
        self._events.append(
            wire.DroneEvent(
                event_id=self._ids(),
                flight_id=self.flight_id,
                seq=self._seq,
                type=type_,
                occurred_at=self._clock(),
                detail=dict(detail or {}),
            )
        )
