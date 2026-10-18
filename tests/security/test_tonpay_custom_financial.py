"""Real mark_paid + durable enqueue integration; provider/settings remain synthetic.

These cases do not execute fulfilment or establish bank settlement.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from paurubot.db.models import Job, Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments import tonpay_custom_runtime as runtime
from paurubot.payments import tonpay_custom_service as mutations
from tests.security.test_payment_intents import intent_engine as intent_engine
from tests.security.test_tonpay_custom_create import creation as creation
from tests.security.test_tonpay_custom_runtime import live as live

pytestmark = pytest.mark.security
# Capture before the imported fixture replaces it. CI executes the real engine module.
_REAL_MARK_PAID = engine.mark_paid


@pytest.fixture
async def banking(live, monkeypatch):  # type: ignore[no-untyped-def]
    maker, owner, pid, client, invoice, cfg, audit, _ = live
    monkeypatch.setattr(engine, "mark_paid", _REAL_MARK_PAID)
    cfg["tonpay_custom_confirm_enabled"] = True
    await mutations.consent(owner, pid, 1, 103000)
    client.check.return_value = replace(invoice, status="completed", paid=True)
    return maker, owner, pid, client, audit


async def saved(banking):  # type: ignore[no-untyped-def]
    maker, _, pid, *_ = banking
    async with maker() as session:
        p = await session.get(Payment, pid)
        jobs = list(await session.scalars(select(Job).where(Job.dedupe_key == f"payment:{pid}:fulfill")))
        return p, jobs


async def test_verified_refresh_commits_paid_and_one_durable_fulfilment(banking):  # type: ignore[no-untyped-def]
    _, owner, pid, _, audit = banking
    result = await runtime.refresh(owner, pid)
    p, jobs = await saved(banking)
    assert result["status"] == "paid"
    assert p.status == PaymentStatus.PAID and p.amount == 100000
    assert p.provider_tx_id == "custom:invoice_" + str(pid)
    assert p.paid_at is not None and p.fulfilled_at is None
    assert len(jobs) == 1 and jobs[0].payload["payment_id"] == pid
    assert jobs[0].shop_id == owner.shop_id and jobs[0].queue == "payments" and jobs[0].kind == "fulfill"
    audit.assert_awaited_once()
    await runtime.refresh(owner, pid)
    assert len((await saved(banking))[1]) == 1
    assert audit.await_count == 1


async def test_enqueue_failure_rolls_back_paid_and_allows_verified_retry(banking, monkeypatch):  # type: ignore[no-untyped-def]
    _, owner, pid, _, audit = banking
    enqueue = engine.queue.enqueue
    monkeypatch.setattr(
        engine.queue, "enqueue", AsyncMock(side_effect=RuntimeError("synthetic enqueue failure"))
    )
    with pytest.raises(RuntimeError, match="synthetic enqueue"):
        await runtime.refresh(owner, pid)
    p, jobs = await saved(banking)
    assert p.status == PaymentStatus.PENDING and p.provider_tx_id is None and not jobs
    assert not p.meta[mutations.STATE_KEY]["paid"]
    audit.assert_not_awaited()
    monkeypatch.setattr(engine.queue, "enqueue", enqueue)
    await runtime.refresh(owner, pid)
    p, jobs = await saved(banking)
    assert p.status == PaymentStatus.PAID and len(jobs) == 1


async def test_audit_failure_rolls_back_payment_and_queued_job(banking):  # type: ignore[no-untyped-def]
    _, owner, pid, _, audit = banking
    audit.side_effect = RuntimeError("synthetic audit failure")
    with pytest.raises(RuntimeError, match="synthetic audit"):
        await runtime.refresh(owner, pid)
    p, jobs = await saved(banking)
    assert p.status == PaymentStatus.PENDING and p.provider_tx_id is None and not jobs
    assert not p.meta[mutations.STATE_KEY]["paid"]
    audit.side_effect = None
    await runtime.refresh(owner, pid)
    assert len((await saved(banking))[1]) == 1


async def test_concurrent_verified_refreshes_enqueue_once(banking):  # type: ignore[no-untyped-def]
    _, owner, pid, client, _ = banking
    barrier = asyncio.Barrier(4)
    reply = client.check.return_value

    async def remote(*args):  # type: ignore[no-untyped-def]
        await asyncio.wait_for(barrier.wait(), 10)
        return reply

    client.check.side_effect = remote
    results = await asyncio.wait_for(
        asyncio.gather(*(runtime.refresh(owner, pid) for _ in range(4)), return_exceptions=True), 20
    )
    assert sum(isinstance(r, dict) and r["status"] == "paid" for r in results) == 1
    assert (
        sum(isinstance(r, mutations.CustomStateError) and r.code == "observation_stale" for r in results) == 3
    )
    p, jobs = await saved(banking)
    assert p.status == PaymentStatus.PAID and len(jobs) == 1


async def test_reused_provider_transaction_disputes_without_new_job(banking):  # type: ignore[no-untyped-def]
    maker, owner, pid, _, audit = banking
    async with maker() as session:
        original = await session.get(Payment, pid)
        session.add(
            Payment(
                shop_id=owner.shop_id,
                customer_id=owner.customer_id,
                provider="tonpay_custom",
                amount=original.amount,
                purpose=original.purpose,
                currency="IRT",
                idempotency_key="synthetic-other-transaction",
                status=PaymentStatus.PAID,
                provider_tx_id="custom:" + original.provider_ref,
                meta={},
            )
        )
        await session.commit()
    result = await runtime.refresh(owner, pid)
    p, jobs = await saved(banking)
    assert result["status"] == "disputed" and p.status == PaymentStatus.DISPUTED and not jobs
    audit.assert_not_awaited()
