"""Flight repository (Rule 2 and Rule 4's flight counterpart).

This module and the migration are the only places that write flight rows. The write methods
accept a :class:`FlightTransitionResult` and nothing else, so a flight status can only be
stored if the flight state machine produced it (and, for a start, ran the permission gate);
the audit event is written in the same transaction.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FlightRecord
from app.db.repositories.audit import SqlAlchemyAuditLog
from app.domain.exceptions import FlightValidationError
from app.domain.flight_state_machine import FlightTransitionResult
from app.domain.models import (
    AuditEventType,
    DenialCode,
    Flight,
    FlightPermission,
    FlightStatus,
    Mission,
    PermissionDenial,
)
from app.services.exceptions import ConcurrentUpdateError


def _to_domain(record: FlightRecord) -> Flight:
    mission = Mission.from_dict(record.mission)
    if mission.checksum != record.mission_checksum:
        raise FlightValidationError(
            f"flight {record.id}: the stored mission does not match its stored checksum"
        )
    permission = None
    if record.permission_id is not None:
        assert record.permission_issuer is not None
        assert record.permission_issued_at is not None
        assert record.permission_valid_from is not None
        assert record.permission_valid_until is not None
        permission = FlightPermission(
            permission_id=record.permission_id,
            issuer=record.permission_issuer,
            flight_id=record.id,
            drone_id=record.drone_id,
            issued_at=record.permission_issued_at,
            valid_from=record.permission_valid_from,
            valid_until=record.permission_valid_until,
        )
    denial = None
    if record.denial_code is not None:
        assert record.denial_reason is not None
        denial = PermissionDenial(DenialCode(record.denial_code), record.denial_reason)
    return Flight.rehydrate(
        id=record.id,
        order_id=record.order_id,
        drone_id=record.drone_id,
        status=FlightStatus(record.status),
        mission=mission,
        created_at=record.created_at,
        updated_at=record.updated_at,
        permission=permission,
        denial=denial,
        acknowledged_checksum=record.acknowledged_checksum,
        failure_reason=record.failure_reason,
        started_at=record.started_at,
    )


def _mutable_columns(flight: Flight) -> dict[str, Any]:
    """Every column a transition may change."""
    permission = flight.permission
    denial = flight.denial
    return {
        "status": flight.status.value,
        "updated_at": flight.updated_at,
        "permission_id": None if permission is None else permission.permission_id,
        "permission_issuer": None if permission is None else permission.issuer,
        "permission_issued_at": None if permission is None else permission.issued_at,
        "permission_valid_from": None if permission is None else permission.valid_from,
        "permission_valid_until": None if permission is None else permission.valid_until,
        "denial_code": None if denial is None else denial.code.value,
        "denial_reason": None if denial is None else denial.reason,
        "acknowledged_checksum": flight.acknowledged_checksum,
        "failure_reason": flight.failure_reason,
        "started_at": flight.started_at,
    }


class SqlAlchemyFlightStore:
    def __init__(self, session: AsyncSession, audit: SqlAlchemyAuditLog) -> None:
        self._session = session
        self._audit = audit

    async def get(self, flight_id: UUID, *, for_update: bool = False) -> Flight | None:
        statement = (
            select(FlightRecord)
            .where(FlightRecord.id == flight_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        record = (await self._session.scalars(statement)).one_or_none()
        return None if record is None else _to_domain(record)

    async def list_for_order(self, order_id: UUID) -> list[Flight]:
        records = await self._session.scalars(
            select(FlightRecord)
            .where(FlightRecord.order_id == order_id)
            .order_by(FlightRecord.created_at, FlightRecord.id)
            .execution_options(populate_existing=True)
        )
        return [_to_domain(record) for record in records]

    async def insert(self, result: FlightTransitionResult) -> bool:
        flight, event = result.flight, result.audit_event
        if event.event_type is not AuditEventType.FLIGHT_CREATED:
            raise ValueError("insert() needs a result produced by FlightStateMachine.create()")

        inserted_id = await self._session.scalar(
            pg_insert(FlightRecord)
            .values(
                id=flight.id,
                order_id=flight.order_id,
                drone_id=flight.drone_id,
                mission=flight.mission.as_dict(),
                mission_checksum=flight.mission.checksum,
                mission_estimated_duration_s=flight.mission.estimated_duration_s,
                created_at=flight.created_at,
                **_mutable_columns(flight),
            )
            # No conflict target: this also covers the "one live flight per drone / per order"
            # partial unique indexes, not just the primary key.
            .on_conflict_do_nothing()
            .returning(FlightRecord.id)
        )
        if inserted_id is None:
            return False
        await self._audit.append(event)
        return True

    async def apply_transition(self, result: FlightTransitionResult) -> None:
        flight, event = result.flight, result.audit_event
        if event.event_type is not AuditEventType.FLIGHT_STATUS_CHANGED or not isinstance(
            event.previous_state, FlightStatus
        ):
            raise ValueError("apply_transition() needs a result produced by transition()")

        # Compare-and-set on the previous status (the lifecycle trigger is the backstop).
        outcome = await self._session.execute(
            update(FlightRecord)
            .where(
                FlightRecord.id == flight.id,
                FlightRecord.status == event.previous_state.value,
            )
            .values(**_mutable_columns(flight))
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", outcome).rowcount != 1:
            raise ConcurrentUpdateError(
                f"flight {flight.id} was not in state '{event.previous_state}' when updated"
            )
        await self._audit.append(event)
