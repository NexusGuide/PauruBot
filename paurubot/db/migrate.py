from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from paurubot.db.session import get_engine

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _upgrade(connection, revision: str) -> None:  # type: ignore[no-untyped-def]
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)


async def upgrade_database(revision: str = "head") -> str | None:
    """Migrate as the schema owner, then align the runtime role's rights (SEC-F26).

    Returns the runtime role that was granted, or None when one role does everything.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from paurubot.config import get_settings
    from paurubot.db.roles import ensure_runtime_role

    settings = get_settings()
    if not settings.database_owner_url:
        async with get_engine().begin() as conn:
            await conn.run_sync(_upgrade, revision)
        return None
    owner = create_async_engine(settings.database_owner_url)
    try:
        async with owner.begin() as conn:
            await conn.run_sync(_upgrade, revision)
            return await ensure_runtime_role(conn, settings.database_url)
    finally:
        await owner.dispose()
