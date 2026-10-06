from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from paurubot.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None, *, pool_size: int = 10) -> AsyncEngine:
    global _engine, _sessionmaker
    url = url or get_settings().database_url
    if not url.startswith("postgresql"):
        raise RuntimeError("PauruBot requires PostgreSQL (postgresql+asyncpg://...)")
    _engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        connect_args={"server_settings": {"application_name": "paurubot", "timezone": "UTC"}},
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def set_sessionmaker(maker: async_sessionmaker[AsyncSession]) -> None:
    """Used by tests to bind every component to one connection."""
    global _sessionmaker
    _sessionmaker = maker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
