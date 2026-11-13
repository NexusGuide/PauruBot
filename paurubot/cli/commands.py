from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from paurubot.cli.main import command
from paurubot.config import get_settings


def _print(ok: bool, label: str, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")


@command("migrate", "apply database migrations")
async def migrate(_: argparse.Namespace) -> int:
    from paurubot.db.migrate import upgrade_database
    from paurubot.db.session import dispose_engine

    role = await upgrade_database()
    await dispose_engine()
    print("database is at the latest revision")
    if role:
        print(f"runtime role {role}: data rights only")
    return 0


# Exit codes of schema-status, read by `pauru rollback` (SEC-F22).
SCHEMA_EXIT = {"same": 0, "older": 3, "newer": 4, "empty": 5}


@command(
    "schema-status",
    "compare the database schema with this release (exit 0 same, 3 older, 4 newer/unknown, 5 empty)",
)
async def schema_status(_: argparse.Namespace) -> int:
    from paurubot.db.migrate import schema_state
    from paurubot.db.session import dispose_engine

    try:
        state, current, heads = await schema_state()
    except Exception as exc:  # the operator script must never read a crash as "compatible"
        print(f"database unreachable: {exc.__class__.__name__}")
        return 6
    finally:
        await dispose_engine()
    print(f"database={current or '-'} release={','.join(heads)} state={state}")
    return SCHEMA_EXIT[state]


async def _checks() -> list[tuple[bool, str, str]]:
    from paurubot.db.session import dispose_engine
    from paurubot.kv import close_redis
    from paurubot.ops import doctor as checks

    try:
        results = await checks.run(with_telegram=False)
    finally:
        await dispose_engine()
        await close_redis()
    return [
        (c.level != "fail", c.key, c.detail if c.level != "warn" else f"warning: {c.detail}") for c in results
    ]


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
    if settings.bot_mode == "webhook":
        from paurubot.api.routes.telegram import webhook_secret

        telegram_bot = await _prepare_bot()
        from paurubot.bot.app import build_dispatcher

        await telegram_bot.set_webhook(
            f"{settings.public_url}/api/telegram/webhook",
            secret_token=webhook_secret(),
            drop_pending_updates=False,
            allowed_updates=build_dispatcher().resolve_used_update_types(),
        )
        from paurubot.core import resale

        async with session_scope() as session:
            for shop, _token in await resale.bots(session):
                try:
                    await resale.point_webhook(shop.id)
                except Exception as exc:  # one broken reseller token must not stop the api
                    print(f"reseller bot {shop.id} webhook skipped: {exc.__class__.__name__}")
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


@command("worker", "run queues and periodic tasks")
async def worker(_: argparse.Namespace) -> int:
    from paurubot.notifications import handlers  # noqa: F401  (registers notification queues)
    from paurubot.worker import jobs  # noqa: F401  (registers queues and periodic tasks)
    from paurubot.worker.runner import Worker

    await Worker().run()
    return 0


async def _prepare_bot():  # type: ignore[no-untyped-def]
    """Create the bot, record its username on the shop and publish the command list."""
    from paurubot.bot.app import set_commands
    from paurubot.core.shops import root_shop
    from paurubot.db.session import session_scope
    from paurubot.i18n import Texts
    from paurubot.notifications.telegram import get_bot

    bot = get_bot()
    me = await bot.me()
    async with session_scope() as session:
        shop = await root_shop(session)
        if shop is not None and shop.bot_username != me.username:
            shop.bot_username = me.username
    await set_commands(bot, Texts("fa"), Texts("en"))
    return bot


def polling_options() -> dict[str, Any]:
    """Updates are handled as tasks, a bounded number at once across every polled bot, so a burst
    queues instead of exhausting the Redis and database pools."""
    return {"handle_as_tasks": True, "tasks_concurrency_limit": get_settings().bot_update_concurrency}


@command("bot", "run the Telegram bot with long polling")
async def bot(_: argparse.Namespace) -> int:
    from paurubot.bot.app import build_dispatcher

    if get_settings().bot_mode == "webhook":
        print("BOT_MODE=webhook: updates are received by the api process")
        return 0
    telegram_bot = await _prepare_bot()
    await telegram_bot.delete_webhook(drop_pending_updates=False)
    dispatcher = build_dispatcher()
    while True:
        # Reseller bots (TEN-04) poll alongside the main bot; a change to them restarts polling.
        extra, fingerprint = await _reseller_bots()
        watcher = asyncio.create_task(_watch_reseller_bots(dispatcher, fingerprint))
        try:
            await dispatcher.start_polling(
                telegram_bot,
                *extra,
                allowed_updates=dispatcher.resolve_used_update_types(),
                **polling_options(),
            )
        finally:
            for extra_bot in extra:
                await extra_bot.session.close()
        if not watcher.done():
            watcher.cancel()
            return 0  # stopped by a signal, not by a change of bots


async def _reseller_bots() -> tuple[list[Any], frozenset[tuple[int, str]]]:
    from paurubot.bot.app import set_commands
    from paurubot.core import resale
    from paurubot.db.session import session_scope
    from paurubot.i18n import Texts
    from paurubot.notifications.telegram import make_bot
    from paurubot.security.tokens import sha256_hex

    async with session_scope() as session:
        rows = await resale.bots(session)
    bots: list[Any] = []
    for shop, token in rows:
        extra = make_bot(token, shop_id=shop.id)
        try:
            await extra.delete_webhook(drop_pending_updates=False)
            await set_commands(extra, Texts("fa"), Texts("en"))
        except Exception as exc:  # one broken reseller token must not stop the others
            print(f"reseller bot skipped: {exc.__class__.__name__}")
            await extra.session.close()
            continue
        bots.append(extra)
    return bots, frozenset((shop.id, sha256_hex(token)) for shop, token in rows)


async def _watch_reseller_bots(dispatcher: Any, fingerprint: frozenset[tuple[int, str]]) -> None:
    from paurubot.core import resale
    from paurubot.db.session import session_scope
    from paurubot.security.tokens import sha256_hex

    while True:
        await asyncio.sleep(60)
        async with session_scope() as session:
            rows = await resale.bots(session)
        if frozenset((shop.id, sha256_hex(token)) for shop, token in rows) != fingerprint:
            await dispatcher.stop_polling()
            return


# --- backups (OPS-05) --------------------------------------------------------------


@command(
    "backup",
    "write an encrypted backup now (add --send to post it to Telegram)",
    lambda p: p.add_argument("--send", action="store_true"),
)
async def backup_cmd(args: argparse.Namespace) -> int:
    from paurubot.ops import backup

    try:
        info = await backup.create(label="cli")
    except backup.BackupError as exc:
        print(f"backup failed: {exc}")
        return 1
    from paurubot.ops import backup_schedule

    backup.prune((await backup_schedule.current()).keep)
    print(f"{info.path} ({info.size} bytes)")
    if args.send:
        from paurubot.ops.delivery import send_backup

        print(f"sent to {await send_backup(info)} chat(s)")
    return 0


@command("backups", "list backups in the backup directory")
async def backups_cmd(_: argparse.Namespace) -> int:
    from paurubot.ops import backup

    for info in backup.listing():
        print(f"{info.created_at:%Y-%m-%d %H:%M}  {info.size:>12}  {info.name}")
    return 0


def _backup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="path to a .pbak archive")
    parser.add_argument(
        "--passphrase-env",
        default="BACKUP_PASSPHRASE",
        help="environment variable holding the passphrase (default BACKUP_PASSPHRASE)",
    )


def _passphrase(args: argparse.Namespace) -> str:
    import os

    return os.environ.get(args.passphrase_env, "") or get_settings().backup_passphrase.get_secret_value()


def _verify_args(parser: argparse.ArgumentParser) -> None:
    _backup_args(parser)
    parser.add_argument("--full", action="store_true", help="also restore into a scratch database")


@command(
    "backup-verify",
    "check a backup's hashes and, with --full, restore it into a scratch database",
    _verify_args,
)
async def backup_verify(args: argparse.Namespace) -> int:
    from pathlib import Path

    from paurubot.ops import backup

    try:
        result = await backup.verify(Path(args.file), passphrase=_passphrase(args), full=args.full)
    except backup.BackupError as exc:
        _print(False, "backup", str(exc))
        return 1
    manifest = result["manifest"]
    _print(True, "decrypted and hashes match", manifest["created_at"])
    _print(True, "database dump readable", f"{result['objects']} objects")
    _print(result["master_key_included"], "master key included")
    if "restored" in result:
        restored = result["restored"]
        _print(True, "test restore", ", ".join(f"{k}={v}" for k, v in restored.items()))
    return 0


def _restore_args(parser: argparse.ArgumentParser) -> None:
    _backup_args(parser)
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument(
        "--write-env", metavar="PATH", help="put the backup's MASTER_KEY into this .env file when it differs"
    )
    parser.add_argument(
        "--print-master-key",
        action="store_true",
        help="print the backup's MASTER_KEY after restoring instead of comparing it with this server",
    )


@command("restore", "replace the database with a backup (stop api, bot and worker first)", _restore_args)
async def restore_cmd(args: argparse.Namespace) -> int:
    from pathlib import Path

    from alembic.script import ScriptDirectory

    from paurubot.db.migrate import alembic_config, upgrade_database
    from paurubot.db.session import dispose_engine
    from paurubot.ops import backup

    path = Path(args.file)
    try:
        info = await backup.verify(path, passphrase=_passphrase(args))
    except backup.BackupError as exc:
        _print(False, "backup", str(exc))
        return 1
    manifest = info["manifest"]
    revision = manifest.get("alembic_revision")
    known = {rev.revision for rev in ScriptDirectory.from_config(alembic_config()).walk_revisions()}
    if revision and revision not in known:
        _print(False, "schema", f"backup is from a newer PauruBot (revision {revision}); update first")
        return 1
    if not args.yes:
        question = f"Replace the current database with the backup from {manifest['created_at']}? [y/N] "
        answer = await asyncio.to_thread(input, question)
        if answer.strip().lower() not in {"y", "yes"}:
            print("canceled")
            return 1
    try:
        contents = await backup.restore(path, passphrase=_passphrase(args))
    except backup.BackupError as exc:
        _print(False, "restore", str(exc))
        return 1
    _print(True, "database restored", manifest["created_at"])
    await upgrade_database()
    await dispose_engine()
    _print(True, "migrations applied")
    if args.print_master_key and contents.master_key:
        print(f"MASTER_KEY={contents.master_key}")
        return 0
    if contents.master_key and not backup.master_key_matches(contents):
        if args.write_env:
            backup.write_env_master_key(Path(args.write_env), contents.master_key)
            _print(True, "MASTER_KEY", f"written to {args.write_env}; restart the services")
        else:
            _print(
                False,
                "MASTER_KEY",
                "differs from this server; rerun with --write-env .env or panel passwords stay unreadable",
            )
            return 3
    return 0


def _import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="MySQL dump (.sql) or SQLite database of the old bot")
    parser.add_argument("--source", choices=["mirza", "faoxima", "wizwiz"], help="detected when omitted")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=PANEL_ID",
        help="old panel name or server id to a PauruBot panel id; repeat per panel",
    )
    parser.add_argument("--shop", type=int, help="shop id (default: the first shop)")
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")


@command("import-bot", "move customers, balances and services over from another bot (MIG-01)", _import_args)
async def import_bot(args: argparse.Namespace) -> int:
    import asyncio
    from pathlib import Path

    from sqlalchemy import select

    from paurubot.db.models import Shop
    from paurubot.db.session import dispose_engine, session_scope
    from paurubot.migration import runner, sources

    path = Path(args.file)
    if not await asyncio.to_thread(path.is_file):
        print(f"no such file: {path}")
        return 2
    name = args.source or await asyncio.to_thread(sources.guess, path)
    extract = await asyncio.to_thread(sources.SOURCES[name], path)
    info = runner.summary(extract)
    print(
        f"source {name}: {info['customers']} customers, {info['services']} services, balance {info['balance_total']}"
    )
    for panel in info["panels"]:
        print(f"  panel {panel['key']!r} ({panel['label']}): {panel['services']} services")
    panel_map: dict[str, int] = {}
    for item in args.map:
        key, _, value = item.rpartition("=")
        if not key or not value.isdigit():
            print(f"bad --map {item!r}; use OLD=PANEL_ID")
            return 2
        panel_map[key] = int(value)
    try:
        async with session_scope() as session:
            shop_id = args.shop or await session.scalar(select(Shop.id).order_by(Shop.id))
            if shop_id is None:
                print("no shop yet; finish setup first")
                return 2
            report = await runner.apply(session, int(shop_id), extract, panel_map, dry_run=not args.apply)
    finally:
        await dispose_engine()
    print(json.dumps(report.as_dict(), indent=2))
    if not args.apply:
        print("dry run: nothing was written; add --apply to import")
    return 0
