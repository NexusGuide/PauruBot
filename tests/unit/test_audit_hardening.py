"""Offline audit regressions; no provider calls or production credentials.

Run alone with --noconftest to avoid the application's PostgreSQL/Redis fixtures.
The cap test selects the real policy function's AST because its containing engine
imports all panel drivers. It is not a DB/concurrency integration test.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from paurubot import kv
from paurubot.auth import staff
from paurubot.core import apps
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.gateways import TonpayProvider
from paurubot.security import tokens
from paurubot.worker import queue, runner

pytestmark = pytest.mark.security
ROOT = Path(__file__).resolve().parents[2]


def invoice(**changes):
    return {
        "invoice_id": "TP-ABC123XYZ0",
        "invoice_url": "https://t.me/TonPaysInvoiceBot?start=inv_TP-ABC123XYZ0",
        "order_id": "1002",
        "request_amount": 50_000,
        "final_amount": 50_037,
        "status": "completed",
        "paid": True,
        **changes,
    }


def payment(**changes):
    return SimpleNamespace(
        **{
            "id": 1002,
            "amount": 50_000,
            "provider_ref": "TP-ABC123XYZ0",
            "provider_amount": 50_037,
            "provider_currency": "IRT",
            **changes,
        }
    )


async def test_tonpay_freezes_quote_and_credits_nominal(monkeypatch):
    provider = TonpayProvider()
    request = AsyncMock(return_value=invoice())
    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(provider, "callback_url", lambda _: "https://shop.example/callback")
    p = payment()
    checkout = await provider.create(p, {})
    assert checkout.provider_amount == 50_037
    p.provider_amount = checkout.provider_amount
    p.provider_currency = checkout.provider_currency
    verified = await provider.verify(p, {}, {"source": "poll"})
    assert verified.paid and verified.amount == 50_000
    assert verified.raw["final_amount"] == 50_037
    assert verified.provider_tx_id == "tonpay:TP-ABC123XYZ0"
    assert request.await_args_list[-1].args[:2] == ("GET", "check/TP-ABC123XYZ0")


@pytest.mark.parametrize(
    "changes",
    [
        {"request_amount": 49_999},
        {"final_amount": None},
        {"final_amount": True},
        {"final_amount": "50037.0"},
        {"order_id": "somebody-else"},
        {"invoice_id": "../other"},
        {"currency": "IRR"},
    ],
)
async def test_tonpay_rejects_invalid_create_quote(monkeypatch, changes):
    provider = TonpayProvider()
    monkeypatch.setattr(provider, "_request", AsyncMock(return_value=invoice(**changes)))
    monkeypatch.setattr(provider, "callback_url", lambda _: "https://shop.example/callback")
    with pytest.raises(PaymentError):
        await provider.create(payment(), {})


@pytest.mark.parametrize(
    "remote",
    [
        {"final_amount": 50_036},
        {"final_amount": 50_038},
        {"request_amount": 50_001},
        {"order_id": "other"},
        {"invoice_id": "TP-OTHER"},
        {"paid": False},
        {"status": "pending"},
        {"currency": "IRR"},
    ],
)
async def test_tonpay_never_trusts_callback_or_changed_settlement(monkeypatch, remote):
    provider = TonpayProvider()
    monkeypatch.setattr(provider, "_request", AsyncMock(return_value=invoice(**remote)))
    result = await provider.verify(payment(), {}, {"json": invoice()})
    assert not result.paid


async def test_tonpay_legacy_quote_is_not_guessed(monkeypatch):
    provider = TonpayProvider()
    monkeypatch.setattr(provider, "_request", AsyncMock(return_value=invoice()))
    assert not (await provider.verify(payment(provider_amount=50_000), {}, {})).paid
    assert not (await provider.verify(payment(provider_amount=None), {}, {})).paid


async def test_tonpay_callback_identity_rejected_before_remote_call(monkeypatch):
    provider = TonpayProvider()
    request = AsyncMock()
    monkeypatch.setattr(provider, "_request", request)
    assert not (await provider.verify(payment(), {}, {"query": {"order_id": "9"}})).paid
    request.assert_not_awaited()


async def test_otp_atomic_consume_has_exactly_one_winner():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await redis.set("otp:code:1", "hash")
        await redis.set("otp:tries:1", "1")
        result = await asyncio.gather(
            *(redis.eval(staff.OTP_CONSUME, 2, "otp:code:1", "otp:tries:1", "hash") for _ in range(20))
        )
        assert sum(result) == 1
        assert await redis.get("otp:code:1") is None
        assert await redis.get("otp:tries:1") is None
    finally:
        await redis.aclose()


async def test_otp_wrong_value_cannot_consume_replacement():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        await redis.set("otp:code:1", "new-hash")
        assert not await redis.eval(staff.OTP_CONSUME, 2, "otp:code:1", "otp:tries:1", "old-hash")
        assert await redis.get("otp:code:1") == "new-hash"
    finally:
        await redis.aclose()


def test_app_cache_separates_asset_selection_and_shop():
    app = {"name": "Client", "repo": "example/client", "pattern": "*arm64*", "platform": "android"}
    original = apps.cache_key(1, app)
    assert original != apps.cache_key(2, app)
    assert original != apps.cache_key(1, {**app, "pattern": "*x86*"})
    assert original != apps.cache_key(1, {**app, "name": "Other"})
    assert original == apps.cache_key(1, dict(app))


async def test_password_workers_are_bounded_and_do_not_block_loop(monkeypatch):
    monkeypatch.setattr(tokens, "_PASSWORD_WORKERS", asyncio.Semaphore(2))
    lock, active, peak = threading.Lock(), 0, 0

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return True

    tasks = [asyncio.create_task(tokens._password_work(work)) for _ in range(6)]
    await asyncio.sleep(0.01)
    assert any(not task.done() for task in tasks)
    assert all(await asyncio.gather(*tasks))
    assert peak == 2


async def test_cancelled_password_keeps_worker_slot_until_thread_finishes(monkeypatch):
    monkeypatch.setattr(tokens, "_PASSWORD_WORKERS", asyncio.Semaphore(1))
    started, release, second = threading.Event(), threading.Event(), threading.Event()

    def first():
        started.set()
        release.wait(timeout=2)

    task = asyncio.create_task(tokens._password_work(first))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        waiter = asyncio.create_task(tokens._password_work(second.set))
        await asyncio.sleep(0.02)
        assert not second.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await waiter
    assert second.is_set()


@pytest.mark.parametrize(
    "enabled,cap,amount,expected",
    [(False, 100, 50, False), (True, 100, 101, False), (True, 100, 100, True), (True, 0, 50, False)],
)
async def test_trusted_customer_cannot_bypass_operator_flag_or_cap(enabled, cap, amount, expected):
    # Execute the actual policy, not a copied implementation. No SQL/history branch is used.
    source = ROOT / "paurubot/payments/engine.py"
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "auto_approvable")
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    env = {"auto_review_customer": AsyncMock(return_value=SimpleNamespace(card_trust="trusted"))}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), env)
    actual = await env["auto_approvable"](
        None, payment(amount=amount), {"card_auto_approve": enabled, "card_auto_max_amount": cap}
    )
    assert actual is expected


def worker_fixture(monkeypatch):
    events = []
    session = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("commit")), rollback=AsyncMock()
    )

    @asynccontextmanager
    async def maker():
        yield session

    async def invoke(handler, session, spec, claimed):
        await handler(session, claimed)

    async def enqueue(*args, **kwargs):
        events.append(("enqueue", args[2], args[3], kwargs))
        return 2

    fake_ops = ModuleType("paurubot.ops")
    fake_ops.metrics = SimpleNamespace(count_job=AsyncMock())
    monkeypatch.setitem(sys.modules, "paurubot.ops", fake_ops)
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(runner, "_with_heartbeat", invoke)
    monkeypatch.setattr(queue, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(queue, "complete", AsyncMock(return_value=True))
    monkeypatch.setattr(queue, "enqueue", enqueue)
    monkeypatch.setattr(queue, "fail", AsyncMock(return_value=True))
    return events


async def test_dead_job_persists_compensation_before_failure_commit(monkeypatch):
    events = worker_fixture(monkeypatch)
    hook = AsyncMock()
    spec = runner.QueueSpec({"work": AsyncMock(side_effect=runner.Permanent("failed"))}, on_dead=hook)
    original = queue.Claimed(9, "work", {"order_id": 42}, 8, 8, 1, "old-token")
    await runner._run_job("provision", spec, original)
    hook.assert_not_awaited()
    pending = next(
        event for event in events if isinstance(event, tuple) and event[1] == runner.DEAD_HOOK_KIND
    )
    assert pending[2]["id"] == 9
    assert pending[2]["payload"] == {"order_id": 42}
    assert pending[3]["dedupe_key"] == "dead-hook:9"
    assert events.index(pending) < len(events) - 1 and events[-1] == "commit"


async def test_durable_hook_restores_original_job_but_never_stale_token(monkeypatch):
    worker_fixture(monkeypatch)
    hook = AsyncMock()
    spec = runner.QueueSpec({}, on_dead=hook)
    job = queue.Claimed(
        10,
        runner.DEAD_HOOK_KIND,
        {
            "id": 9,
            "kind": "work",
            "payload": {"order_id": 42},
            "attempts": 8,
            "max_attempts": 8,
            "error": "failed",
        },
        1,
        8,
        1,
        "new-token",
    )
    await runner._run_job("provision", spec, job)
    restored = hook.await_args.args[1]
    assert restored.id == 9 and restored.payload == {"order_id": 42} and restored.token == ""
    queue.complete.assert_awaited_once_with(hook.await_args.args[0], job)


async def test_failed_compensation_does_not_recursively_enqueue_itself(monkeypatch):
    events = worker_fixture(monkeypatch)
    spec = runner.QueueSpec({}, on_dead=AsyncMock(side_effect=RuntimeError("unavailable")))
    job = queue.Claimed(
        10,
        runner.DEAD_HOOK_KIND,
        {"id": 9, "kind": "work", "payload": {}, "attempts": 8, "max_attempts": 8, "error": "failed"},
        8,
        8,
        1,
        "token",
    )
    await runner._run_job("provision", spec, job)
    assert not any(isinstance(event, tuple) and event[1] == runner.DEAD_HOOK_KIND for event in events)


async def test_verify_code_end_to_end_rejects_concurrent_reuse(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    account = SimpleNamespace(id=7, shop_id=1, is_active=True, last_login_at=None)
    session = SimpleNamespace(scalar=AsyncMock(return_value=account))
    monkeypatch.setattr(kv, "get_redis", lambda: redis)
    monkeypatch.setattr(staff.login_guard, "check", AsyncMock())
    monkeypatch.setattr(staff.login_guard, "succeeded", AsyncMock())
    monkeypatch.setattr(staff.login_guard, "failed", AsyncMock())
    monkeypatch.setattr(staff, "record", AsyncMock())
    try:
        await redis.set("otp:code:7", staff._code_hash(7, "123456"))
        result = await asyncio.gather(
            *(staff.verify_code(session, "admin", "123456", "192.0.2.1") for _ in range(12)),
            return_exceptions=True,
        )
        assert sum(item is account for item in result) == 1
        assert sum(isinstance(item, staff.AuthError) for item in result) == 11
        staff.login_guard.succeeded.assert_awaited_once()
    finally:
        await redis.aclose()


@pytest.mark.parametrize("oversize", [False, True])
async def test_app_mirror_caps_actual_stream_and_invalidates_changed_asset(monkeypatch, oversize):
    import httpx

    release = apps.Release("v1", "new.apk", "https://example.com/new.apk", 1)
    monkeypatch.setattr(apps, "latest", AsyncMock(return_value=release))
    monkeypatch.setattr(
        apps,
        "cached",
        AsyncMock(
            return_value={
                "version": "v1",
                "asset": "old.apk",
                "url": "https://example.com/old.apk",
                "file_id": "old-id",
            }
        ),
    )
    monkeypatch.setattr(apps, "TELEGRAM_LIMIT", 8)
    store = SimpleNamespace(set=AsyncMock())
    monkeypatch.setattr(kv, "get_redis", lambda: store)
    bot = SimpleNamespace(
        send_document=AsyncMock(return_value=SimpleNamespace(document=SimpleNamespace(file_id="new-id")))
    )
    telegram = ModuleType("paurubot.notifications.telegram")
    telegram.get_bot = lambda: bot
    monkeypatch.setitem(sys.modules, "paurubot.notifications.telegram", telegram)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456"
            if oversize:
                yield b"789012"

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    app = {"name": "Client", "repo": "example/client", "pattern": "*.apk", "platform": "android"}
    assert await apps.refresh(1, [app], 1, transport=transport) == 1
    if oversize:
        bot.send_document.assert_not_awaited()
    else:
        bot.send_document.assert_awaited_once()
    import json

    entry = json.loads(store.set.await_args.args[1])
    assert entry["file_id"] == (None if oversize else "new-id")


@pytest.mark.parametrize("fault", [None, "database", "redis", "old_schema", "empty_schema"])
async def test_readiness_requires_database_redis_and_image_schema(monkeypatch, fault):
    from alembic.script import ScriptDirectory
    from fastapi.responses import JSONResponse

    from paurubot.db import migrate
    from paurubot.db import session as db_session

    source = ROOT / "paurubot/api/app.py"
    tree = ast.parse(source.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "readyz")
    node.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    env = {"JSONResponse": JSONResponse, "__version__": "test"}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), env)
    revision = [] if fault == "empty_schema" else ["old" if fault == "old_schema" else "current"]
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("database unavailable") if fault == "database" else None),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: revision)),
    )

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(migrate, "alembic_config", lambda: None)
    monkeypatch.setattr(
        ScriptDirectory, "from_config", lambda cfg: SimpleNamespace(get_heads=lambda: ["current"])
    )
    redis = SimpleNamespace(
        ping=AsyncMock(side_effect=RuntimeError("redis unavailable") if fault == "redis" else None)
    )
    monkeypatch.setattr(kv, "get_redis", lambda: redis)
    result = await env["readyz"]()
    assert result.status_code == (200 if fault is None else 503)
    assert b"unavailable" not in result.body


def installer_function(name):
    # Execute only the actual function body, with fake commands; never source installer main.
    text = (ROOT / "install.sh").read_text()
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + 2
    return text[start:end]


@pytest.mark.parametrize("mode", ["ok", "missing_cosign", "no_digest", "bad_signature", "pull_failed"])
def test_installer_fails_closed_on_unverified_release(mode):
    import subprocess

    stub = r"""
set -Eeuo pipefail
REPO=MenderBaga/PauruBot
registry_login() { :; }
ok() { :; }
die() { exit 17; }
compose() { echo UNSAFE_BUILD; exit 18; }
command() {
  if [[ "$MODE" == missing_cosign && "$*" == '-v cosign' ]]; then return 1; fi
  builtin command "$@"
}
docker() {
  if [[ "$1" == pull ]]; then [[ "$MODE" != pull_failed ]]; return; fi
  if [[ "$MODE" != no_digest ]]; then echo 'ghcr.io/menderbaga/paurubot@sha256:synthetic'; fi
}
cosign() { [[ "$MODE" != bad_signature ]]; }
# The installer now fetches cosign itself; when even that fails, nothing unverified may run.
install_cosign() { die "cosign could not be installed"; }
"""
    script = (
        stub
        + f"\nMODE={mode}\n"
        + installer_function("fetch_image")
        + '\nfetch_image ghcr.io/menderbaga/paurubot:edge\nprintf "%s" "$PINNED_IMAGE"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if mode == "ok" else 17)
    assert "UNSAFE_BUILD" not in result.stdout
    if mode == "ok":
        assert result.stdout == "ghcr.io/menderbaga/paurubot@sha256:synthetic"


def test_installer_backup_failure_prevents_source_image_or_environment_changes(tmp_path):
    import subprocess

    env_file = tmp_path / ".env"
    env_file.touch()
    stub = r"""
set -Eeuo pipefail
need_root() { :; }
env_get() { echo stable; }
resolve_target() { TARGET_REF=main; }
say() { :; }
ok() { :; }
die() { exit 17; }
compose() { return 1; }
fetch_source() { echo UNSAFE_SOURCE; }
fetch_image() { echo UNSAFE_IMAGE; }
env_set() { echo UNSAFE_ENVIRONMENT; }
"""
    script = (
        stub + '\nENV_FILE="$1"\nBOLD=""\nRESET=""\n' + installer_function("cmd_update") + "\ncmd_update\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "test", str(env_file)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 17
    assert "UNSAFE" not in result.stdout


async def test_existing_ledger_account_is_read_without_unique_key_write():
    from paurubot.ledger import accounts

    existing = SimpleNamespace(id=9, balance=200)
    session = SimpleNamespace(scalar=AsyncMock(return_value=existing), execute=AsyncMock())
    assert await accounts.wallet_account(session, 1, 7) is existing
    session.scalar.assert_awaited_once()
    session.execute.assert_not_awaited()


async def test_missing_ledger_account_keeps_conflict_safe_creation():
    from paurubot.ledger import accounts

    winner = SimpleNamespace(id=9, balance=0)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, winner]), execute=AsyncMock())
    assert await accounts.wallet_account(session, 1, 7) is winner
    assert session.scalar.await_count == 2
    session.execute.assert_awaited_once()
    assert "ON CONFLICT" in str(session.execute.await_args.args[0])
