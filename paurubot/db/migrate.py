from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from paurubot.db.session import get_engine

ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _upgrade(connection) -> None:  # type: ignore[no-untyped-def]
    cfg = _config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def upgrade_database() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(_upgrade)
