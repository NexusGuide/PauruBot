"""Environment analyser (ADM-08): one list of checks for the CLI `doctor` and the admin panel."""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select, text

from paurubot import __version__, kv
from paurubot.config import get_settings
from paurubot.db.base import utcnow

Level = Literal["ok", "warn", "fail"]
HEARTBEAT_KEY = "worker:heartbeat"


@dataclass(slots=True)
class Check:
    key: str
    level: Level
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _free_bytes(directory: str) -> int:
    target = Path(directory)
    probe = target if target.exists() else target.parent if target.parent.exists() else Path("/")
    return shutil.disk_usage(probe).free


async def heartbeat() -> None:
    await kv.get_redis().set(HEARTBEAT_KEY, str(time.time()), ex=3600)


def periodic_info() -> str:
    """One line per registered worker periodic task with its last and next run (additive detail)."""
    from paurubot.worker.runner import PERIODIC

    if not PERIODIC:
        try:
            import paurubot.worker.jobs  # noqa: F401 (registers the periodic tasks)
        except Exception:
            return "periodic registry unavailable"
    if not PERIODIC:
        return "no periodic tasks registered"
    now = time.monotonic()
    parts = []
    for task in PERIODIC:
        if task.next_run:
            last_ago = max(0, int(now - (task.next_run - task.every)))
            parts.append(
                f"{task.name}: every {int(task.every)}s, next in {max(0, int(task.next_run - now))}s,"
                f" last ~{last_ago}s ago"
            )
        else:
            parts.append(f"{task.name}: every {int(task.every)}s, not run yet")
    return "; ".join(parts)


async def run(*, with_telegram: bool = True) -> list[Check]:
    from alembic.script import ScriptDirectory

    from paurubot.db.migrate import alembic_config
    from paurubot.db.models import Job, JobStatus, Panel
    from paurubot.db.session import get_engine, get_sessionmaker

    settings = get_settings()
    checks: list[Check] = []

    def add(key: str, level: Level, detail: str) -> None:
        checks.append(Check(key, level, detail))

    problems = settings.validate_runtime()
    add(
        "configuration",
        "fail" if problems else "ok",
        "; ".join(problems) or f"{settings.environment}, v{__version__}",
    )
    add("master_key", "ok" if settings.master_key.get_secret_value() else "fail", "MASTER_KEY")
    if settings.public_url.startswith("https://"):
        add("public_url", "ok", settings.public_url)
    else:
        add("public_url", "warn", "PUBLIC_URL is not https; the Mini App, gateways and branded links need it")

    db_ok = False
    try:
        async with get_engine().connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar()
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            db_now = (await conn.execute(text("SELECT now()"))).scalar()
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        add("postgresql", "ok", f"server {version}")
        add("migrations", "ok" if current == head else "fail", f"current {current}, latest {head}")
        skew = abs((utcnow() - db_now).total_seconds()) if db_now else 0.0
        add("clock", "ok" if skew < 5 else "warn", f"app and database clocks differ by {skew:.1f}s")
        db_ok = True
    except Exception as exc:  # report, never crash the doctor
        add("postgresql", "fail", exc.__class__.__name__)

    try:
        pong = await kv.get_redis().ping()
        add("redis", "ok" if pong else "fail", "ping ok" if pong else "no answer")
        beat = await kv.get_redis().get(HEARTBEAT_KEY)
        age = time.time() - float(beat) if beat else None
        if age is None:
            add("worker", "fail", "no heartbeat; is the worker container running?")
        else:
            add("worker", "ok" if age < 120 else "fail", f"last heartbeat {int(age)}s ago")
    except Exception as exc:
        add("redis", "fail", exc.__class__.__name__)

    add("periodic", "ok", periodic_info())

    if db_ok:
        async with get_sessionmaker()() as session:
            dead = int(
                await session.scalar(select(func.count(Job.id)).where(Job.status == JobStatus.DEAD)) or 0
            )
            oldest = await session.scalar(
                select(func.min(Job.run_at)).where(Job.status == JobStatus.QUEUED, Job.run_at <= utcnow())
            )
            lag = (utcnow() - oldest).total_seconds() if oldest else 0.0
            add("dead_jobs", "ok" if dead == 0 else "warn", f"{dead} jobs failed for good")
            add("queue_lag", "ok" if lag < 300 else "warn", f"oldest due job waits {int(lag)}s")
            panels = list(
                await session.execute(select(Panel.name, Panel.is_healthy).where(Panel.is_active.is_(True)))
            )
            down = [name for name, healthy in panels if healthy is False]
            if not panels:
                add("panels", "warn", "no active panel")
            else:
                add(
                    "panels",
                    "fail" if down else "ok",
                    ", ".join(down) + " down" if down else f"{len(panels)} up",
                )

    if settings.backup_passphrase.get_secret_value():
        from paurubot.ops.backup import listing

        items = listing()
        if not items:
            add("backups", "warn", "no backup yet")
        else:
            backup_age = utcnow() - items[0].created_at
            level: Level = "ok" if backup_age < timedelta(hours=36) else "warn"
            hours = int(backup_age.total_seconds() // 3600)
            add("backups", level, f"latest {items[0].name}, {hours}h old")
    else:
        add("backups", "fail", "BACKUP_PASSPHRASE is empty; backups cannot be encrypted")
    free = await asyncio.to_thread(_free_bytes, settings.backup_dir)
    add("disk", "ok" if free > 2 * 1024**3 else "warn", f"{free / 1024**3:.1f} GB free for backups")
    add(
        "pg_dump",
        "ok" if shutil.which("pg_dump") else "warn",
        "found" if shutil.which("pg_dump") else "pg_dump is missing; backups will fail",
    )

    token = settings.bot_token.get_secret_value()
    if not token:
        add("telegram", "fail", "BOT_TOKEN is empty")
    elif with_telegram:
        try:
            from paurubot.notifications.telegram import get_bot

            bot = get_bot()
            me = await bot.get_me()
            add("telegram", "ok", f"@{me.username}")
            if settings.bot_mode == "webhook":
                info = await bot.get_webhook_info()
                if not info.url:
                    add("webhook", "fail", "no webhook registered")
                elif info.last_error_message:
                    add(
                        "webhook",
                        "warn",
                        f"{info.pending_update_count} pending, last error: {info.last_error_message}",
                    )
                else:
                    add("webhook", "ok", f"{info.pending_update_count} pending")
        except Exception as exc:
            add("telegram", "fail", exc.__class__.__name__)
    return checks
