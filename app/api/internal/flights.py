"""Internal flight endpoints: read a flight, and send the drone a high-level command.

Rule 1: the cloud never steers a drone. The only commands are start, pause, resume,
return-to-home and land, and each takes nothing but the flight's id (and an optional
free-text reason). There is no endpoint that accepts a position, a heading, a speed or a
status. Start and resume run the Rule 2 permission gate; pause, return-to-home and land
never do, because stopping must never be blocked by paperwork.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body

from app.api.deps import DispatchServiceDep
from app.api.internal.schemas import CommandReason, FlightOut
from app.domain.models import EventSource

router = APIRouter(prefix="/internal/flights", tags=["internal"])

ReasonBody = Annotated[CommandReason | None, Body()]


def _reason(body: CommandReason | None) -> str:
    return body.reason if body is not None and body.reason else "operator request"


@router.get("/{flight_id}", response_model=FlightOut)
async def get_flight(flight_id: UUID, service: DispatchServiceDep) -> FlightOut:
    return FlightOut.from_domain(await service.get_flight(flight_id))


@router.post("/{flight_id}/start", response_model=FlightOut)
async def start_flight(flight_id: UUID, service: DispatchServiceDep) -> FlightOut:
    """Start a staged flight (mission uploaded). Runs the permission gate."""
    flight = await service.start_flight(flight_id, source=EventSource.API)
    return FlightOut.from_domain(flight)


@router.post("/{flight_id}/pause", response_model=FlightOut)
async def pause_flight(flight_id: UUID, service: DispatchServiceDep) -> FlightOut:
    flight = await service.pause_flight(flight_id, source=EventSource.API)
    return FlightOut.from_domain(flight)


@router.post("/{flight_id}/resume", response_model=FlightOut)
async def resume_flight(flight_id: UUID, service: DispatchServiceDep) -> FlightOut:
    """Resume a paused flight. Runs the permission gate again."""
    flight = await service.resume_flight(flight_id, source=EventSource.API)
    return FlightOut.from_domain(flight)


@router.post("/{flight_id}/return-to-home", response_model=FlightOut)
async def return_to_home(
    flight_id: UUID, service: DispatchServiceDep, body: ReasonBody = None
) -> FlightOut:
    flight = await service.return_to_home(flight_id, source=EventSource.API, reason=_reason(body))
    return FlightOut.from_domain(flight)


@router.post("/{flight_id}/land", response_model=FlightOut)
async def land(flight_id: UUID, service: DispatchServiceDep, body: ReasonBody = None) -> FlightOut:
    flight = await service.land(flight_id, source=EventSource.API, reason=_reason(body))
    return FlightOut.from_domain(flight)
