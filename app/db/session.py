"""Async engine and session factory."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(url: str, *, echo: bool = False, **engine_kwargs: Any) -> AsyncEngine:
    return create_async_engine(url, echo=echo, pool_pre_ping=True, **engine_kwargs)


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: results are mapped to domain objects, never kept as ORM state.
    return async_sessionmaker(engine, expire_on_commit=False)
