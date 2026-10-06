from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/paurubot_test")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("MASTER_KEY", "test-master-key-0123456789-abcdefghijklmnop")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-token-for-unit-tests-only-xxxxxxxx")
os.environ.setdefault("LOG_JSON", "false")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from paurubot import kv  # noqa: E402
from paurubot.config import get_settings  # noqa: E402
from paurubot.db import session as db_session  # noqa: E402


def _migrate(connection) -> None:  # type: ignore[no-untyped-def]
    from alembic import command

    from paurubot.db.migrate import alembic_config

    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def engine():  # type: ignore[no-untyped-def]
    url = get_settings().database_url
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(_migrate)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """Each test runs inside one outer transaction that is rolled back afterwards.

    Code under test may commit freely: commits become savepoint releases.
    """
    async with engine.connect() as conn:
        outer = await conn.begin()
        maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        db_session.set_sessionmaker(maker)
        async with maker() as sess:
            yield sess
        await outer.rollback()
    db_session.set_sessionmaker(None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
async def redis_clean() -> AsyncIterator[None]:
    kv.set_redis(None)
    client = kv.get_redis()
    await client.flushdb()
    yield
    await client.flushdb()
    await kv.close_redis()
