"""Drone registry repository.

This module and the migration are the only places that write drone rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DroneRecord
from app.db.spatial import latitude_of, longitude_of, point
from app.domain.models import Drone, DroneStatus, Location
from app.domain.models.drone import check_status_change
from app.services.exceptions import ConcurrentUpdateError


def _drone_select() -> Any:
    return select(
        DroneRecord.id,
        DroneRecord.name,
        DroneRecord.status,
        longitude_of(DroneRecord.home).label("home_lon"),
        latitude_of(DroneRecord.home).label("home_lat"),
        DroneRecord.max_payload_kg,
        DroneRecord.max_range_km,
        DroneRecord.battery_percent,
        DroneRecord.created_at,
        DroneRecord.updated_at,
    )


def _to_domain(row: Row[Any]) -> Drone:
    return Drone.rehydrate(
        id=row.id,
        name=row.name,
        status=DroneStatus(row.status),
        home=Location(latitude=row.home_lat, longitude=row.home_lon),
        max_payload_kg=row.max_payload_kg,
        max_range_km=row.max_range_km,
        battery_percent=row.battery_percent,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyDroneStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, drone: Drone) -> bool:
        inserted_id = await self._session.scalar(
            pg_insert(DroneRecord)
            .values(
                id=drone.id,
                name=drone.name,
                status=drone.status.value,
                home=point(drone.home),
                max_payload_kg=drone.max_payload_kg,
                max_range_km=drone.max_range_km,
                battery_percent=drone.battery_percent,
                created_at=drone.created_at,
                updated_at=drone.updated_at,
            )
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(DroneRecord.id)
        )
        return inserted_id is not None

    async def get(self, drone_id: UUID, *, for_update: bool = False) -> Drone | None:
        statement = _drone_select().where(DroneRecord.id == drone_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else _to_domain(row)

    async def list_all(self, *, status: DroneStatus | None = None) -> list[Drone]:
        statement = _drone_select().order_by(DroneRecord.name)
        if status is not None:
            statement = statement.where(DroneRecord.status == status.value)
        return [_to_domain(row) for row in (await self._session.execute(statement)).all()]

    async def list_available(self, *, lock: bool = False) -> list[Drone]:
        statement = (
            _drone_select()
            .where(DroneRecord.status == DroneStatus.AVAILABLE.value)
            .order_by(DroneRecord.id)
        )
        if lock:
            statement = statement.with_for_update(skip_locked=True)
        return [_to_domain(row) for row in (await self._session.execute(statement)).all()]

    async def set_battery(self, drone_id: UUID, battery_percent: float, *, now: datetime) -> bool:
        outcome = await self._session.execute(
            update(DroneRecord)
            .where(DroneRecord.id == drone_id)
            .values(battery_percent=battery_percent, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return cast("CursorResult[Any]", outcome).rowcount == 1

    async def apply_status_change(self, before: Drone, after: Drone) -> None:
        check_status_change(before, after)
        outcome = await self._session.execute(
            update(DroneRecord)
            .where(DroneRecord.id == before.id, DroneRecord.status == before.status.value)
            .values(status=after.status.value, updated_at=after.updated_at)
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", outcome).rowcount != 1:
            raise ConcurrentUpdateError(
                f"drone {before.id} was not in state '{before.status}' when updated"
            )
