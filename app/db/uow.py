"""SQLAlchemy unit of work: one transaction per ``async with`` block."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.audit import SqlAlchemyAuditLog
from app.db.repositories.drones import SqlAlchemyDroneStore
from app.db.repositories.flights import SqlAlchemyFlightStore
from app.db.repositories.orders import SqlAlchemyOrderStore
from app.db.repositories.webhooks import SqlAlchemyWebhookStore


class SqlAlchemyUnitOfWork:
    orders: SqlAlchemyOrderStore
    audit: SqlAlchemyAuditLog
    drones: SqlAlchemyDroneStore
    flights: SqlAlchemyFlightStore
    webhooks: SqlAlchemyWebhookStore

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        self._session = session
        self.audit = SqlAlchemyAuditLog(session)
        self.webhooks = SqlAlchemyWebhookStore(session)
        self.orders = SqlAlchemyOrderStore(session, self.audit, self.webhooks)
        self.drones = SqlAlchemyDroneStore(session)
        self.flights = SqlAlchemyFlightStore(session, self.audit)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Closing the session rolls back anything that was not committed.
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("commit() called outside the unit of work")
        await self._session.commit()
