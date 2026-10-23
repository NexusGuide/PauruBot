"""Mini App transfer consent, durable replay, tenant guards and actual PG transactions."""

from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from paurubot.api.routes import webapp_transfers as routes
from paurubot.api.webapp_auth import WebAppContext, webapp_context
from paurubot.core import settings as shop_settings
from paurubot.core import transfer_quotes as quotes
from paurubot.core import transfers
from paurubot.core.transfers import TransferError
from paurubot.db.base import Base
from paurubot.db.models import Customer, LedgerJournal, Shop, ShopSetting
from paurubot.ledger.accounts import system_account, wallet_account, wallet_balance
from paurubot.ledger.posting import reconcile, transfer

pytestmark = pytest.mark.security


async def setup(session):  # type: ignore[no-untyped-def]
    shop = Shop(title="Transfer regression")
    session.add(shop)
    await session.flush()
    a = Customer(shop_id=shop.id, telegram_id=100, first_name="Sender", username="sender")
    b = Customer(shop_id=shop.id, telegram_id=200, first_name="Recipient", username="recipient")
    session.add_all([a, b])
    await session.flush()
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "wallet_transfer_enabled": True,
            "wallet_transfer_min": 10,
            "wallet_transfer_daily_max": 100,
        },
    )
    bank = await system_account(session, shop.id, "adjustments")
    account = await wallet_account(session, shop.id, a.id)
    await transfer(
        session,
        shop_id=shop.id,
        source_id=bank.id,
        target_id=account.id,
        amount=200,
        kind="test_seed",
        idempotency_key=f"seed:{shop.id}:{secrets.token_hex(8)}",
    )
    return shop, a, b


async def consent(session, a, amount=50):  # type: ignore[no-untyped-def]
    return await quotes.quote(session, a, "@recipient", amount, "IRT")


async def test_quote_does_not_transfer_and_confirm_replays(session):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    assert q["recipient"]["telegram_id"] == b.telegram_id
    assert await wallet_balance(session, shop.id, a.id) == 200
    first = await quotes.confirm(session, a, q["token"], "IRT")
    second = await quotes.confirm(session, a, q["token"], "IRT")
    assert first["journal_id"] == second["journal_id"] and second["replayed"]
    assert await wallet_balance(session, shop.id, a.id) == 150
    assert await wallet_balance(session, shop.id, b.id) == 50
    assert await reconcile(session, shop.id) == []


@pytest.mark.parametrize("mode", ["sender", "shop", "currency", "signature", "unicode", "oversize"])
async def test_quote_cannot_be_rebound_or_forged(session, mode):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    token, sid, uid, currency = q["token"], shop.id, a.id, "IRT"
    if mode == "sender":
        uid = b.id
    if mode == "shop":
        sid += 1
    if mode == "currency":
        currency = "USD"
    if mode == "signature":
        token = token[:-1] + ("0" if token[-1] != "0" else "1")
    if mode == "unicode":
        token = token.split(".")[0] + ".سلام"
    if mode == "oversize":
        token = "a" * 2049
    with pytest.raises(TransferError, match="transfer_quote_invalid"):
        quotes.decode(token, sid, uid, currency)


@pytest.mark.parametrize(
    "field,value",
    [("amount", True), ("amount", 0), ("amount", -1), ("amount", "50"), ("shop", 1), ("role", "owner")],
)
async def test_quote_http_rejects_coercion_and_authority_fields(session, field, value):  # type: ignore[no-untyped-def]
    shop, a, _ = await setup(session)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/webapp")
    app.dependency_overrides[webapp_context] = lambda: WebAppContext(session, shop, a, {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/webapp/wallet/transfers/quote", json={"recipient": "@recipient", "amount": 50, field: value}
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "recipient", ["²", "9223372036854775808", "0", "-1", "@" + "a" * 33, "1 OR 1=1", "@<tag>"]
)
async def test_quote_http_rejects_unsafe_recipient_input(session, recipient):  # type: ignore[no-untyped-def]
    shop, a, _ = await setup(session)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/webapp")
    app.dependency_overrides[webapp_context] = lambda: WebAppContext(session, shop, a, {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/webapp/wallet/transfers/quote", json={"recipient": recipient, "amount": 50}
        )
    assert response.status_code == 422


@pytest.mark.parametrize("target", ["@sender", "100", "@unknown", "-1"])
async def test_invalid_and_self_recipient(session, target):  # type: ignore[no-untyped-def]
    _, a, _ = await setup(session)
    with pytest.raises(TransferError):
        await quotes.quote(session, a, target, 50, "IRT")


async def test_other_shop_recipient_is_not_discoverable(session):  # type: ignore[no-untyped-def]
    _, a, b = await setup(session)
    other = Shop(title="Other")
    session.add(other)
    await session.flush()
    b.shop_id = other.id
    await session.flush()
    with pytest.raises(TransferError, match="transfer_no_recipient"):
        await consent(session, a)


@pytest.mark.parametrize("change", ["sender_ban", "recipient_ban", "disabled", "daily", "minimum", "funds"])
async def test_policy_and_parties_rechecked_at_confirm(session, change):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    if change == "sender_ban":
        a.is_banned = True
    if change == "recipient_ban":
        b.is_banned = True
    if change == "disabled":
        await shop_settings.set_many(session, shop.id, {"wallet_transfer_enabled": False})
    if change == "daily":
        await shop_settings.set_many(session, shop.id, {"wallet_transfer_daily_max": 20})
    if change == "minimum":
        await shop_settings.set_many(session, shop.id, {"wallet_transfer_min": 60})
    if change == "funds":
        source = await wallet_account(session, shop.id, a.id)
        bank = await system_account(session, shop.id, "adjustments")
        await transfer(
            session,
            shop_id=shop.id,
            source_id=source.id,
            target_id=bank.id,
            amount=190,
            kind="test_spend",
            idempotency_key=f"spend:{shop.id}",
        )
    await session.flush()
    with pytest.raises(TransferError):
        await quotes.confirm(session, a, q["token"], "IRT")
    assert await wallet_balance(session, shop.id, b.id) == 0


async def test_database_policy_change_is_not_hidden_by_process_cache(session):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    # Update the database directly: deliberately do NOT invalidate the process settings cache.
    await session.execute(
        update(ShopSetting)
        .where(ShopSetting.shop_id == shop.id, ShopSetting.key == "wallet_transfer_enabled")
        .values(value=False)
    )
    assert (await shop_settings.load(session, shop.id))["wallet_transfer_enabled"] is True
    with pytest.raises(TransferError, match="transfer_off"):
        await quotes.confirm(session, a, q["token"], "IRT")
    assert await wallet_balance(session, shop.id, b.id) == 0


async def test_expired_unposted_fails_but_posted_quote_replays(session, monkeypatch):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    pending = await consent(session, a)
    await quotes.confirm(session, a, q["token"], "IRT")
    monkeypatch.setattr(quotes, "time", SimpleNamespace(time=lambda: pending["expires_at"]))
    assert (await quotes.confirm(session, a, q["token"], "IRT"))["replayed"]
    with pytest.raises(TransferError, match="transfer_quote_expired"):
        await quotes.confirm(session, a, pending["token"], "IRT")
    assert await wallet_balance(session, shop.id, b.id) == 50


async def test_rollback_does_not_erase_consent_or_leave_money(session):  # type: ignore[no-untyped-def]
    shop, a, b = await setup(session)
    q = await consent(session, a)
    nested = await session.begin_nested()
    await quotes.confirm(session, a, q["token"], "IRT")
    await nested.rollback()
    assert await wallet_balance(session, shop.id, b.id) == 0
    result = await quotes.confirm(session, a, q["token"], "IRT")
    assert not result["replayed"]
    assert await reconcile(session, shop.id) == []


async def test_confirm_http_commits_and_replays(session):  # type: ignore[no-untyped-def]
    shop, a, _ = await setup(session)
    q = await consent(session, a)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/webapp")
    app.dependency_overrides[webapp_context] = lambda: WebAppContext(session, shop, a, {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        p = await client.get("/api/webapp/wallet/transfer-policy")
        assert p.json()["enabled"] and p.headers["cache-control"] == "no-store"
        first = await client.post("/api/webapp/wallet/transfers/confirm", json={"token": q["token"]})
        again = await client.post("/api/webapp/wallet/transfers/confirm", json={"token": q["token"]})
    assert first.status_code == again.status_code == 200
    assert first.json()["journal_id"] == again.json()["journal_id"]
    assert again.json()["balance"] == 150 and again.json()["replayed"]


@pytest.fixture
async def transfer_engine(engine):  # type: ignore[no-untyped-def]
    # Independent real commits must not leak shops/journals into the suite's public schema.
    schema = "transfer_" + secrets.token_hex(8)
    isolated = engine.execution_options(schema_translate_map={None: schema})
    shop_settings.invalidate()
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    try:
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield isolated
    finally:
        shop_settings.invalidate()
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))


@pytest.mark.parametrize("mode", ["same_quote", "different_quotes", "bot"])
async def test_independent_committed_transactions_serialize_replay_and_daily_cap(transfer_engine, mode):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(transfer_engine, expire_on_commit=False)
    async with maker() as session:
        shop, a, b = await setup(session)
        shop_id, sender_id, recipient_id = shop.id, a.id, b.id
        first = await consent(session, a, 70)
        second = first if mode == "same_quote" else await consent(session, a, 70)
        await session.commit()
    start = asyncio.Event()

    async def run(token, bot=False):  # type: ignore[no-untyped-def]
        async with maker() as session:
            sender = await session.get(Customer, sender_id)
            await start.wait()
            try:
                if bot:
                    recipient = await session.get(Customer, recipient_id)
                    await transfers.send(session, sender, recipient, 70, key=f"bot-race:{shop_id}")
                    result = {"replayed": False}
                else:
                    result = await quotes.confirm(session, sender, token, "IRT")
                await session.commit()
                return result
            except TransferError as exc:
                await session.rollback()
                return exc.key

    tasks = [
        asyncio.create_task(run(first["token"])),
        asyncio.create_task(run(second["token"], mode == "bot")),
    ]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    if mode == "same_quote":
        assert all(isinstance(r, dict) for r in results)
        assert sum(bool(r["replayed"]) for r in results) == 1
    else:
        assert sum(r == "transfer_daily_max" for r in results) == 1
    async with maker() as session:
        assert await wallet_balance(session, shop_id, sender_id) == 130
        assert await wallet_balance(session, shop_id, recipient_id) == 70
        total = await session.scalar(
            select(func.count())
            .select_from(LedgerJournal)
            .where(LedgerJournal.shop_id == shop_id, LedgerJournal.kind == "transfer")
        )
        assert total == 1
        assert await reconcile(session, shop_id) == []


async def test_existing_wallet_read_does_not_invert_missing_wallet_creation_lock(transfer_engine):  # type: ignore[no-untyped-def]
    """Force the update-vs-speculative-insert cycle from concurrent confirmations."""
    from paurubot.db.models import LedgerAccount

    maker = async_sessionmaker(transfer_engine, expire_on_commit=False)
    async with maker() as session:
        shop, sender, recipient = await setup(session)
        shop_id, sender_id, recipient_id = shop.id, sender.id, recipient.id
        await session.commit()
    async with maker() as holder, maker() as creator:
        # Keep a newer, uncommitted version of the existing source wallet.
        await holder.execute(
            update(LedgerAccount)
            .where(LedgerAccount.shop_id == shop_id, LedgerAccount.customer_id == sender_id)
            .values(balance=LedgerAccount.balance)
        )
        # The second transaction owns the not-yet-committed recipient account.
        created = await wallet_account(creator, shop_id, recipient_id)
        recipient_account_id = created.id

        async def holder_needs_recipient():  # type: ignore[no-untyped-def]
            found = await wallet_account(holder, shop_id, recipient_id)
            await holder.commit()
            return found.id

        async def creator_needs_sender():  # type: ignore[no-untyped-def]
            found = await wallet_account(creator, shop_id, sender_id)
            assert found.balance == 200
            await creator.commit()
            return found.customer_id

        tasks = [asyncio.create_task(holder_needs_recipient()), asyncio.create_task(creator_needs_sender())]
        try:
            result = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
            assert result == [recipient_account_id, sender_id]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with maker() as session:
        assert (
            await session.scalar(
                select(func.count(LedgerAccount.id)).where(
                    LedgerAccount.shop_id == shop_id, LedgerAccount.customer_id.in_([sender_id, recipient_id])
                )
            )
            == 2
        )
        assert await wallet_balance(session, shop_id, sender_id) == 200
        assert await wallet_balance(session, shop_id, recipient_id) == 0
        assert await reconcile(session, shop_id) == []
