from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from paurubot.cli.main import command
from paurubot.config import get_settings


def _print(ok: bool, label: str, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")


@command("migrate", "apply database migrations")
async def migrate(_: argparse.Namespace) -> int:
    from paurubot.db.migrate import upgrade_database
    from paurubot.db.session import dispose_engine

    await upgrade_database()
    await dispose_engine()
    print("database is at the latest revision")
    return 0


async def _checks() -> list[tuple[bool, str, str]]:
    from alembic.script import ScriptDirectory

    from paurubot.db.migrate import alembic_config
    from paurubot.db.session import dispose_engine, get_engine
    from paurubot.kv import close_redis, get_redis

    settings = get_settings()
    results: list[tuple[bool, str, str]] = []
    problems = settings.validate_runtime()
    results.append((not problems, "configuration", "; ".join(problems) or settings.environment))
    results.append(
        (
            bool(settings.bot_token.get_secret_value()),
            "bot token",
            "set" if settings.bot_token.get_secret_value() else "BOT_TOKEN is empty",
        )
    )
    try:
        async with get_engine().connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar()
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        results.append((True, "postgresql", f"server {version}"))
        results.append((current == head, "migrations", f"current={current} head={head}"))
    except Exception as exc:  # report, never crash the doctor
        results.append((False, "postgresql", exc.__class__.__name__))
    finally:
        await dispose_engine()
    try:
        pong = await get_redis().ping()
        results.append((bool(pong), "redis", "ping ok"))
    except Exception as exc:
        results.append((False, "redis", exc.__class__.__name__))
    finally:
        await close_redis()
    if settings.backup_passphrase.get_secret_value():
        results.append((True, "backup passphrase", "set"))
    else:
        results.append(
            (False, "backup passphrase", "BACKUP_PASSPHRASE is empty; backups cannot be encrypted")
        )
    return results


@command(
    "doctor",
    "check configuration, database, migrations and redis",
    lambda p: p.add_argument("--json", action="store_true"),
)
async def doctor(args: argparse.Namespace) -> int:
    results = await _checks()
    if args.json:
        print(json.dumps([{"ok": ok, "check": label, "detail": detail} for ok, label, detail in results]))
    else:
        for ok, label, detail in results:
            _print(ok, label, detail)
    return 0 if all(ok for ok, _, _ in results) else 1


@command("gen-master-key", "print a new random MASTER_KEY")
async def gen_master_key(_: argparse.Namespace) -> int:
    from paurubot.security.secretbox import generate_master_key

    print(generate_master_key())
    return 0
