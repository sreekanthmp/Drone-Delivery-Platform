"""Internal fleet endpoints: register drones, read them, and two operational commands.

There is no endpoint that accepts a status. ``docked`` and ``maintenance`` are verbs that ask
the dispatch service to make a change, which it refuses unless the drone's state machine
allows it and the drone has no live flight (Rule 4 applies to drones and flights as well).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import DispatchServiceDep
from app.api.internal.schemas import DroneOut, DroneRegistration
from app.domain.models import Location

router = APIRouter(prefix="/internal/drones", tags=["internal"])


@router.post("", response_model=DroneOut, status_code=201)
async def register_drone(body: DroneRegistration, service: DispatchServiceDep) -> DroneOut:
    drone = await service.register_drone(
        name=body.name,
        home=Location(latitude=body.home.latitude, longitude=body.home.longitude),
        max_payload_kg=body.max_payload_kg,
        max_range_km=body.max_range_km,
        battery_percent=body.battery_percent,
    )
    return DroneOut.from_domain(drone)


@router.get("", response_model=list[DroneOut])
async def list_drones(service: DispatchServiceDep) -> list[DroneOut]:
    return [DroneOut.from_domain(drone) for drone in await service.list_drones()]


@router.get("/{drone_id}", response_model=DroneOut)
async def get_drone(drone_id: UUID, service: DispatchServiceDep) -> DroneOut:
    return DroneOut.from_domain(await service.get_drone(drone_id))


@router.post("/{drone_id}/docked", response_model=DroneOut)
async def confirm_docked(drone_id: UUID, service: DispatchServiceDep) -> DroneOut:
    """Operations confirms the drone is back at its dock and may take deliveries again."""
    return DroneOut.from_domain(await service.confirm_docked(drone_id))


@router.post("/{drone_id}/maintenance", response_model=DroneOut)
async def set_maintenance(drone_id: UUID, service: DispatchServiceDep) -> DroneOut:
    """Take a drone out of service."""
    return DroneOut.from_domain(await service.set_maintenance(drone_id))
