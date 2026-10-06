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


@command("api", "run the HTTP API (admin panel, Mini App, webhooks)")
async def api(_: argparse.Namespace) -> int:
    import uvicorn

    from paurubot.api.app import create_app
    from paurubot.core.setup import issue_token
    from paurubot.core.shops import is_set_up
    from paurubot.db.session import session_scope

    settings = get_settings()
    problems = settings.validate_runtime()
    if problems and settings.is_production:
        for problem in problems:
            print(f"configuration error: {problem}")
        return 2
    async with session_scope() as session:
        needs_setup = not await is_set_up(session)
    if needs_setup:
        token = await issue_token()
        base = settings.public_url or f"http://{settings.api_host}:{settings.api_port}"
        print(
            f"\n  First-run setup: open {base}/admin/setup?token={token}\n  (valid for 24 hours)\n",
            flush=True,
        )
    config = uvicorn.Config(
        create_app(),
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=False,
        server_header=False,
        log_config=None,
        access_log=False,
    )
    await uvicorn.Server(config).serve()
    return 0


@command("setup-token", "issue a new one-time setup token (only before setup is completed)")
async def setup_token(_: argparse.Namespace) -> int:
    from paurubot.core.setup import issue_token
    from paurubot.core.shops import is_set_up
    from paurubot.db.session import session_scope

    async with session_scope() as session:
        if await is_set_up(session):
            print("setup is already completed")
            return 1
    token = await issue_token()
    base = get_settings().public_url or "http://SERVER:8080"
    print(f"{base}/admin/setup?token={token}")
    return 0


@command("reconcile", "verify ledger balances against journal entries")
async def reconcile(_: argparse.Namespace) -> int:
    from paurubot.db.session import session_scope
    from paurubot.ledger.posting import reconcile as run

    async with session_scope() as session:
        drift = await run(session)
    if not drift:
        print("ledger is consistent")
        return 0
    for item in drift:
        print(f"DRIFT account={item.code} cached={item.cached} actual={item.actual}")
    return 1
