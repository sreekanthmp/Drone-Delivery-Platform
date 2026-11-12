"""A helper that walks real flights through their lifecycle via the stores.

The flight-guard tests need flights that were stored the proper way (state machine ->
store, one committed transaction per step) so they can then try to *bypass* that path with
raw SQL. By default it uses the real clock, because the database trigger judges permission
validity against the database's clock; pass a different clock to build flights whose
permissions are already in the past.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.compliance.mock_digital_sky import MockDigitalSky
from app.domain.flight_state_machine import FlightStateMachine, FlightTransitionResult
from app.domain.models import (
    DenialCode,
    EventSource,
    Flight,
    FlightStatus,
    Mission,
    PermissionDenial,
    PermissionRequest,
)
from app.services.ports import UnitOfWorkFactory
from tests.conftest import build_mission

S = FlightStatus

# The happy path, and the shortest way to every other status.
_PATHS: dict[FlightStatus, list[FlightStatus]] = {
    S.PLANNED: [],
    S.PERMISSION_GRANTED: [S.PERMISSION_GRANTED],
    S.PERMISSION_DENIED: [S.PERMISSION_DENIED],
    S.MISSION_UPLOADED: [S.PERMISSION_GRANTED, S.MISSION_UPLOADED],
    S.UPLOAD_FAILED: [S.PERMISSION_GRANTED, S.UPLOAD_FAILED],
    S.CANCELLED: [S.CANCELLED],
    S.STARTED: [S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED],
    S.PAUSED: [S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.PAUSED],
    S.COMPLETED: [S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.COMPLETED],
    S.ABORTED: [S.PERMISSION_GRANTED, S.MISSION_UPLOADED, S.STARTED, S.ABORTED],
}


class FlightFlow:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        clock: Callable[[], datetime] | None = None,
        validity_minutes: float = 15,
    ) -> None:
        from datetime import timedelta

        clock = clock or (lambda: datetime.now(UTC))
        self._uow_factory = uow_factory
        self.machine = FlightStateMachine(clock=clock)
        self.sky = MockDigitalSky(clock=clock, validity=timedelta(minutes=validity_minutes))
        self._clock = clock

    async def plan(self, order_id: UUID, drone_id: UUID, mission: Mission | None = None) -> Flight:
        result = self.machine.create(
            order_id=order_id,
            drone_id=drone_id,
            mission=mission or build_mission(),
            source=EventSource.DISPATCH,
        )
        async with self._uow_factory() as uow:
            assert await uow.flights.insert(result), "a live flight already exists"
            await uow.commit()
        return result.flight

    async def advance(self, flight: Flight, target: FlightStatus) -> Flight:
        """Apply one transition with sensible context and store it. Returns the new flight."""
        result = await self._transition(flight, target)
        async with self._uow_factory() as uow:
            await uow.flights.apply_transition(result)
            await uow.commit()
        return result.flight

    async def flight_in(
        self,
        status: FlightStatus,
        order_id: UUID,
        drone_id: UUID,
        mission: Mission | None = None,
    ) -> Flight:
        """Plan a flight and walk it, one committed transaction per step, to ``status``."""
        flight = await self.plan(order_id, drone_id, mission)
        for target in _PATHS[status]:
            flight = await self.advance(flight, target)
        return flight

    async def _transition(self, flight: Flight, target: FlightStatus) -> FlightTransitionResult:
        context: dict[str, Any] = {}
        if target is S.PERMISSION_GRANTED:
            decision = await self.sky.request_permission(
                PermissionRequest.from_mission(
                    flight_id=flight.id,
                    order_id=flight.order_id,
                    drone_id=flight.drone_id,
                    mission=flight.mission,
                    requested_start=self._clock(),
                )
            )
            context["permission"] = decision.permission  # type: ignore[union-attr]
        elif target is S.PERMISSION_DENIED:
            context["denial"] = PermissionDenial(DenialCode.REJECTED, "test denial")
        elif target is S.MISSION_UPLOADED:
            context["acknowledged_checksum"] = flight.mission.checksum
        elif target is S.STARTED:
            assert flight.permission is not None
            context["validation"] = await self.sky.validate_permission(
                flight.permission.permission_id
            )
        elif target in {S.UPLOAD_FAILED, S.CANCELLED, S.ABORTED}:
            context["reason"] = "test"
        return self.machine.transition(flight, target, source=EventSource.DISPATCH, **context)
