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


async def schema_state(conn=None) -> tuple[str, str | None, list[str]]:  # type: ignore[no-untyped-def]
    """Where the database stands against this release's migrations (SEC-F22).

    "same": at this release's head; "older": a revision this release knows, before its head
    (its migrate moves it forward); "newer": a revision this release does not know, so it was
    migrated by a later release and this one must not run on it; "empty": no schema yet.
    """
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError
    from sqlalchemy import text

    scripts = ScriptDirectory.from_config(alembic_config())
    heads = sorted(scripts.get_heads())
    query = text("SELECT version_num FROM alembic_version")
    if conn is None:
        async with get_engine().connect() as own:
            exists = await own.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
            current = await own.scalar(query) if exists else None
    else:
        exists = await conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        current = await conn.scalar(query) if exists else None
    if not current:
        return "empty", None, heads
    if current in heads:
        return "same", current, heads
    try:
        known = scripts.get_revision(current) is not None
    except (CommandError, KeyError):
        known = False
    return ("older" if known else "newer"), current, heads
