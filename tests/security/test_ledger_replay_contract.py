"""The same ledger key must mean the same financial effect, not merely the same kind."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paurubot.core import settings as shop_settings
from paurubot.db.models import LedgerEntry, LedgerJournal
from paurubot.ledger import posting
from paurubot.ledger.accounts import system_account, wallet_account, wallet_balance
from tests import factories
from tests.conftest import _migrate

pytestmark = pytest.mark.security


async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    other = await factories.customer(session, shop)
    source = await system_account(session, shop.id, "bank")
    target = await wallet_account(session, shop.id, customer.id)
    other_target = await wallet_account(session, shop.id, other.id)
    return shop, customer, source, target, other_target


@pytest.mark.parametrize("changed", ["amount", "account", "reference_type", "reference_id", "reverses_id"])
async def test_same_key_different_financial_request_is_rejected(session, changed) -> None:  # type: ignore[no-untyped-def]
    shop, customer, source, target, other = await env(session)
    params = dict(
        shop_id=shop.id,
        kind="test_topup",
        idempotency_key="replay-contract",
        lines=[posting.Line(source.id, -100), posting.Line(target.id, 100)],
        reference_type="payment",
        reference_id=42,
    )
    original = await posting.post(session, **params)
    if changed == "amount":
        params["lines"] = [posting.Line(source.id, -200), posting.Line(target.id, 200)]
    elif changed == "account":
        params["lines"] = [posting.Line(source.id, -100), posting.Line(other.id, 100)]
    elif changed == "reference_type":
        params["reference_type"] = "order"
    elif changed == "reference_id":
        params["reference_id"] = 43
    elif changed == "reverses_id":
        params["reverses_id"] = original.journal.id
    with pytest.raises(posting.LedgerError, match="idempotency key"):
        await posting.post(session, **params)
    assert await wallet_balance(session, shop.id, customer.id) == 100
    assert await session.scalar(select(func.count(LedgerJournal.id))) == 1
    assert await session.scalar(select(func.count(LedgerEntry.id))) == 2
    assert await posting.reconcile(session, shop.id) == []
    # Rejection stays inside its savepoint; the caller's transaction remains usable.
    params.update(
        lines=[posting.Line(source.id, -100), posting.Line(target.id, 100)],
        reference_type="payment",
        reference_id=42,
        reverses_id=None,
    )
    replay = await posting.post(session, **params)
    assert not replay.created and replay.journal.id == original.journal.id


async def test_equivalent_merged_lines_and_metadata_are_a_valid_replay(session) -> None:  # type: ignore[no-untyped-def]
    shop, _, source, target, _ = await env(session)
    first = await posting.post(
        session,
        shop_id=shop.id,
        kind="topup",
        idempotency_key="equivalent",
        lines=[posting.Line(source.id, -100), posting.Line(target.id, 100)],
        memo="first",
        actor="worker:1",
    )
    second = await posting.post(
        session,
        shop_id=shop.id,
        kind="topup",
        idempotency_key="equivalent",
        lines=[
            posting.Line(target.id, 70),
            posting.Line(source.id, -100),
            posting.Line(target.id, 40),
            posting.Line(target.id, -10),
        ],
        memo="retry",
        actor="worker:2",
    )
    assert first.created and not second.created and first.journal.id == second.journal.id
    assert second.journal.memo == "first" and second.journal.actor == "worker:1"


@pytest.fixture(scope="module")
async def replay_engine(engine):  # type: ignore[no-untyped-def]
    schema = f"test_replay_{uuid4().hex}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_async_engine(engine.url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with isolated.begin() as connection:
            await connection.run_sync(_migrate)
        shop_settings.invalidate()
        yield isolated
    finally:
        await isolated.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        shop_settings.invalidate()


@pytest.mark.parametrize("amounts", [[100, 100, 100, 100], [100, 200, 100, 200]])
async def test_concurrent_same_key_only_accepts_one_financial_meaning(replay_engine, amounts) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(replay_engine, expire_on_commit=False)
    async with maker() as session:
        shop, customer, source, target, _ = await env(session)
        shop_id, customer_id, source_id, target_id = shop.id, customer.id, source.id, target.id
        await session.commit()
    key = f"race:{uuid4().hex}"
    barrier = asyncio.Barrier(len(amounts))

    async def post(amount: int) -> tuple[int, bool] | None:
        async with maker() as session:
            await barrier.wait()
            try:
                result = await posting.post(
                    session,
                    shop_id=shop_id,
                    kind="topup",
                    idempotency_key=key,
                    lines=[posting.Line(source_id, -amount), posting.Line(target_id, amount)],
                )
                await session.commit()
                return amount, result.created
            except posting.LedgerError:
                await session.rollback()
                return None

    results = await asyncio.wait_for(asyncio.gather(*(post(amount) for amount in amounts)), timeout=15)
    accepted = [result for result in results if result is not None]
    assert len({result[0] for result in accepted}) == 1
    assert sum(result[1] for result in accepted) == 1
    assert len(accepted) == amounts.count(accepted[0][0])
    async with maker() as session:
        assert await wallet_balance(session, shop_id, customer_id) == accepted[0][0]
        assert (
            await session.scalar(
                select(func.count(LedgerJournal.id)).where(LedgerJournal.idempotency_key == key)
            )
            == 1
        )
        assert await posting.reconcile(session, shop_id) == []
