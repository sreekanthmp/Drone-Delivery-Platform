"""Response schemas of the internal API (platform format, not any partner's)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.dispatch.service import DispatchResult
from app.domain.models import (
    Drone,
    DroneStatus,
    Flight,
    FlightStatus,
    Location,
    Mission,
    Order,
    OrderStatus,
    Package,
    PermissionDenial,
)
from app.realtime.models import Alert


class LocationOut(BaseModel):
    latitude: float
    longitude: float

    @classmethod
    def from_domain(cls, location: Location) -> LocationOut:
        return cls(latitude=location.latitude, longitude=location.longitude)


class PackageOut(BaseModel):
    weight_kg: float
    description: str

    @classmethod
    def from_domain(cls, package: Package) -> PackageOut:
        return cls(weight_kg=package.weight_kg, description=package.description)


class OrderOut(BaseModel):
    id: UUID
    client_id: str
    external_order_id: str
    status: OrderStatus
    pickup: LocationOut
    dropoff: LocationOut
    package: PackageOut
    drone_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, order: Order) -> OrderOut:
        return cls(
            id=order.id,
            client_id=order.client_id,
            external_order_id=order.external_order_id,
            status=order.status,
            pickup=LocationOut.from_domain(order.pickup),
            dropoff=LocationOut.from_domain(order.dropoff),
            package=PackageOut.from_domain(order.package),
            drone_id=order.drone_id,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class OrderStatusOut(BaseModel):
    order_id: UUID
    status: OrderStatus
    is_terminal: bool
    updated_at: datetime

    @classmethod
    def from_domain(cls, order: Order) -> OrderStatusOut:
        return cls(
            order_id=order.id,
            status=order.status,
            is_terminal=order.status.is_terminal,
            updated_at=order.updated_at,
        )


# --- drones ----------------------------------------------------------------------------------


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float
    longitude: float


class DroneRegistration(BaseModel):
    """Register a drone. A new drone is always ``available`` at its home dock."""

    model_config = ConfigDict(extra="forbid")

    name: str
    home: LocationIn
    max_payload_kg: float
    max_range_km: float
    battery_percent: float


class DroneOut(BaseModel):
    id: UUID
    name: str
    status: DroneStatus
    home: LocationOut
    max_payload_kg: float
    max_range_km: float
    battery_percent: float
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, drone: Drone) -> DroneOut:
        return cls(
            id=drone.id,
            name=drone.name,
            status=drone.status,
            home=LocationOut.from_domain(drone.home),
            max_payload_kg=drone.max_payload_kg,
            max_range_km=drone.max_range_km,
            battery_percent=drone.battery_percent,
            created_at=drone.created_at,
            updated_at=drone.updated_at,
        )


# --- flights ---------------------------------------------------------------------------------


class WaypointOut(BaseModel):
    kind: str
    location: LocationOut
    altitude_m: float


class MissionOut(BaseModel):
    """The predefined mission uploaded to the drone (Rule 1: this is all the cloud ever sends)."""

    mission_id: UUID
    waypoints: list[WaypointOut]
    cruise_speed_mps: float
    estimated_duration_s: float
    checksum: str

    @classmethod
    def from_domain(cls, mission: Mission) -> MissionOut:
        return cls(
            mission_id=mission.mission_id,
            waypoints=[
                WaypointOut(
                    kind=w.kind.value,
                    location=LocationOut.from_domain(w.location),
                    altitude_m=w.altitude_m,
                )
                for w in mission.waypoints
            ],
            cruise_speed_mps=mission.cruise_speed_mps,
            estimated_duration_s=mission.estimated_duration_s,
            checksum=mission.checksum,
        )


class PermissionOut(BaseModel):
    permission_id: str
    issuer: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime


class DenialOut(BaseModel):
    code: str
    reason: str

    @classmethod
    def from_domain(cls, denial: PermissionDenial) -> DenialOut:
        return cls(code=denial.code.value, reason=denial.reason)


class FlightOut(BaseModel):
    id: UUID
    order_id: UUID
    drone_id: UUID
    status: FlightStatus
    mission: MissionOut
    permission: PermissionOut | None
    denial: DenialOut | None
    acknowledged_checksum: str | None
    failure_reason: str | None
    started_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, flight: Flight) -> FlightOut:
        permission = flight.permission
        return cls(
            id=flight.id,
            order_id=flight.order_id,
            drone_id=flight.drone_id,
            status=flight.status,
            mission=MissionOut.from_domain(flight.mission),
            permission=(
                None
                if permission is None
                else PermissionOut(
                    permission_id=permission.permission_id,
                    issuer=permission.issuer,
                    issued_at=permission.issued_at,
                    valid_from=permission.valid_from,
                    valid_until=permission.valid_until,
                )
            ),
            denial=None if flight.denial is None else DenialOut.from_domain(flight.denial),
            acknowledged_checksum=flight.acknowledged_checksum,
            failure_reason=flight.failure_reason,
            started_at=flight.started_at,
            created_at=flight.created_at,
            updated_at=flight.updated_at,
        )


class CommandReason(BaseModel):
    """The only input a flight command takes besides the flight's id (Rule 1)."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, min_length=1, max_length=300)


class DispatchOut(BaseModel):
    """What a dispatch did. ``outcome`` is the result; a refusal is a normal, retryable one."""

    outcome: str
    succeeded: bool
    order: OrderOut
    flight: FlightOut | None
    drone: DroneOut | None
    rejections: dict[str, list[str]]
    """For ``no_drone_available``: each drone that was considered, and why it was rejected."""

    detail: str | None

    @classmethod
    def from_domain(cls, result: DispatchResult) -> DispatchOut:
        return cls(
            outcome=result.outcome.value,
            succeeded=result.succeeded,
            order=OrderOut.from_domain(result.order),
            flight=None if result.flight is None else FlightOut.from_domain(result.flight),
            drone=None if result.drone is None else DroneOut.from_domain(result.drone),
            rejections={
                str(drone_id): [reason.value for reason in reasons]
                for drone_id, reasons in result.rejections.items()
            },
            detail=result.detail,
        )


# --- alerts ----------------------------------------------------------------------------------


class AlertOut(BaseModel):
    kind: str
    drone_id: UUID | None
    flight_id: UUID | None
    order_id: UUID | None
    message: str
    raised_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, alert: Alert) -> AlertOut:
        return cls(
            kind=alert.kind.value,
            drone_id=alert.drone_id,
            flight_id=alert.flight_id,
            order_id=alert.order_id,
            message=alert.message,
            raised_at=alert.raised_at,
            resolved_at=alert.resolved_at,
        )


class AlertsOut(BaseModel):
    live: bool
    """False when no live view (Redis) is configured: there is nothing to report."""

    active: list[AlertOut]
    recent: list[AlertOut]

    @classmethod
    def of(cls, active: list[Alert], recent: list[Alert]) -> AlertsOut:
        return cls(
            live=True,
            active=[AlertOut.from_domain(a) for a in active],
            recent=[AlertOut.from_domain(a) for a in recent],
        )
