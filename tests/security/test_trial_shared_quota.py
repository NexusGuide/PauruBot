"""Shared trial quota must be checked under a transaction-scoped PostgreSQL lock."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from paurubot.core import settings as shop_settings
from paurubot.core import trials
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Job, Order, Shop, TrialGrant
from tests import factories
from tests.conftest import _migrate

pytestmark = pytest.mark.security


@pytest.fixture(scope="module")
async def trial_engine(engine):  # type: ignore[no-untyped-def]
    # Real commits are needed for concurrency. A disposable schema avoids residue in
    # other tests without disabling append-only ledger triggers to DELETE a shop.
    schema = f"test_trial_{uuid4().hex}"
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


@pytest.fixture
async def trial_env(trial_engine):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(trial_engine, expire_on_commit=False)

    async def create(
        *, quota: int = 1, customers: int = 2, shop_id: int | None = None
    ) -> tuple[int, list[int]]:
        async with maker() as session:
            # These tests do not post money; no system-ledger fixtures are needed.
            shop = Shop(id=shop_id, title="Isolated shared trial quota")
            session.add(shop)
            await session.flush()
            await factories.panel(session, shop)
            ids = [(await factories.customer(session, shop)).id for _ in range(customers)]
            await shop_settings.set_many(
                session, shop.id, {"trial_enabled": True, "trial_daily_quota": quota}
            )
            await session.commit()
            return shop.id, ids

    return maker, create


async def test_lock_supports_bigint_shop_ids(trial_env) -> None:  # type: ignore[no-untyped-def]
    maker, create = trial_env
    _, ids = await create(shop_id=2**40)
    async with maker() as session:
        await trials.request(session, await session.get(Customer, ids[0]))
        await session.commit()
        with pytest.raises(trials.TrialError, match="trial_quota_reached"):
            await trials.request(session, await session.get(Customer, ids[1]))


@pytest.mark.parametrize(("contenders", "quota"), [(2, 1), (8, 3), (4, 0)])
async def test_distinct_customers_cannot_overbook_shared_quota(
    trial_env, monkeypatch, contenders, quota
) -> None:  # type: ignore[no-untyped-def]
    maker, create = trial_env
    shop_id, customer_ids = await create(quota=quota, customers=contenders)
    original = trials.eligibility
    barrier = asyncio.Barrier(contenders)
    first_checks: set[int] = set()

    async def checked(session, customer):  # type: ignore[no-untyped-def]
        result = await original(session, customer)
        if customer.id not in first_checks:
            assert result is None
            first_checks.add(customer.id)
            # All real initial checks pass before any request can insert its grant.
            # A correct implementation rechecks after obtaining the shared lock.
            await barrier.wait()
        return result

    monkeypatch.setattr(trials, "eligibility", checked)

    async def attempt(customer_id: int) -> str:
        async with maker() as session:
            customer = await session.get(Customer, customer_id)
            try:
                await trials.request(session, customer)
                await session.commit()
                return "reserved"
            except trials.TrialError as exc:
                await session.rollback()
                return exc.key

    outcomes = await asyncio.wait_for(asyncio.gather(*(attempt(cid) for cid in customer_ids)), timeout=15)
    accepted = quota or contenders
    assert outcomes.count("reserved") == accepted
    assert outcomes.count("trial_quota_reached") == contenders - accepted
    async with maker() as session:
        for model in (TrialGrant, Order, Job):
            assert (
                await session.scalar(select(func.count(model.id)).where(model.shop_id == shop_id)) == accepted
            )


async def test_rollback_and_failed_provisioning_release_reservations(trial_env) -> None:  # type: ignore[no-untyped-def]
    maker, create = trial_env
    shop_id, ids = await create()
    async with maker() as session:
        await trials.request(session, await session.get(Customer, ids[0]))
        await session.rollback()
    # Rollback releases the transaction lock, grant, order and queued job together.
    async with maker() as session:
        order = await asyncio.wait_for(
            trials.request(session, await session.get(Customer, ids[1])), timeout=5
        )
        grant_id = order.params["grant_id"]
        await session.commit()
        assert (
            await session.scalar(select(func.count(TrialGrant.id)).where(TrialGrant.shop_id == shop_id)) == 1
        )
        await trials.release(session, grant_id)
        await session.commit()
    async with maker() as session:
        assert await trials.eligibility(session, await session.get(Customer, ids[0])) is None
        await trials.request(session, await session.get(Customer, ids[0]))
        await session.commit()
        assert (
            await session.scalar(select(func.count(TrialGrant.id)).where(TrialGrant.shop_id == shop_id)) == 1
        )


async def test_other_shop_is_not_blocked_by_an_uncommitted_reservation(trial_env) -> None:  # type: ignore[no-untyped-def]
    maker, create = trial_env
    _, first_ids = await create()
    _, second_ids = await create()
    async with maker() as first:
        await trials.request(first, await first.get(Customer, first_ids[0]))
        try:
            async with maker() as second:
                order = await asyncio.wait_for(
                    trials.request(second, await second.get(Customer, second_ids[0])), timeout=5
                )
                await second.commit()
                assert order.id is not None
        finally:
            await first.rollback()


async def test_quota_remains_rolling_24_hours_and_counts_previous_generation(trial_env) -> None:  # type: ignore[no-untyped-def]
    maker, create = trial_env
    shop_id, ids = await create(customers=3)
    async with maker() as session:
        old = TrialGrant(
            shop_id=shop_id, customer_id=ids[0], generation=1, created_at=utcnow() - timedelta(hours=25)
        )
        recent = TrialGrant(
            shop_id=shop_id, customer_id=ids[1], generation=1, created_at=utcnow() - timedelta(hours=23)
        )
        session.add_all([old, recent])
        await shop_settings.set_many(session, shop_id, {"trial_generation": 2})
        await session.commit()
        customer = await session.get(Customer, ids[2])
        assert await trials.eligibility(session, customer) == "trial_quota_reached"
        await trials.release(session, recent.id)
        await session.commit()
        # A grant from 25h ago does not consume today's rolling quota; a new round alone
        # would not have removed the 23h grant from that quota.
        await trials.request(session, customer)
        await session.commit()
