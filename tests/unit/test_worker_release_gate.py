"""Lease ownership, cancellation, FIFO modification gates and complete parked scans."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from paurubot import kv
from paurubot.db.models import Job, JobStatus, Order, OrderKind, OrderStatus
from paurubot.ops import metrics
from paurubot.provisioning import lifecycle
from paurubot.worker import jobs, queue, runner
from tests import factories


def _claimed() -> queue.Claimed:
    return queue.Claimed(1, "test", {}, 1, 1, None, "owner")


def _session():
    return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())


def _maker(session):
    @asynccontextmanager
    async def maker():
        yield session

    return maker


@pytest.mark.parametrize("complete", [False, True])
async def test_completion_metric_requires_live_claim(monkeypatch, complete) -> None:
    session = _session()
    handler, fail, counted = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))
    monkeypatch.setattr(queue, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(queue, "complete", AsyncMock(return_value=complete))
    monkeypatch.setattr(queue, "fail", fail)
    monkeypatch.setattr(metrics, "count_job", counted)
    await runner._run_job("test", runner.QueueSpec({"test": handler}), _claimed())
    handler.assert_awaited_once()
    fail.assert_not_awaited()
    if complete:
        counted.assert_awaited_once_with("test", "done")
    else:
        counted.assert_not_awaited()
        session.rollback.assert_awaited_once()


async def test_expired_claim_never_starts_handler(monkeypatch) -> None:
    session, handler = _session(), AsyncMock()
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))
    monkeypatch.setattr(queue, "renew", AsyncMock(return_value=False))
    await runner._run_job("test", runner.QueueSpec({"test": handler}), _claimed())
    handler.assert_not_awaited()
    session.rollback.assert_awaited_once()


async def test_stale_failure_never_reports_dead_or_runs_hook(monkeypatch) -> None:
    session = _session()
    handler = AsyncMock(side_effect=runner.Permanent("failed"))
    hook, counted, notify = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))
    monkeypatch.setattr(queue, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(queue, "fail", AsyncMock(return_value=None))
    monkeypatch.setattr(queue, "enqueue", notify)
    monkeypatch.setattr(metrics, "count_job", counted)
    await runner._run_job("test", runner.QueueSpec({"test": handler}, on_dead=hook), _claimed())
    hook.assert_not_awaited()
    notify.assert_not_awaited()
    counted.assert_not_awaited()


async def test_service_contention_does_not_use_failure_budget(monkeypatch) -> None:
    session = _session()
    fail, hook, deferred = AsyncMock(), AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))
    monkeypatch.setattr(queue, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(queue, "defer", deferred)
    monkeypatch.setattr(queue, "fail", fail)
    job = _claimed()
    await runner._run_job(
        "test", runner.QueueSpec({"test": AsyncMock(side_effect=runner.Defer("busy"))}, on_dead=hook), job
    )
    deferred.assert_awaited_once_with(session, job, "busy")
    fail.assert_not_awaited()
    hook.assert_not_awaited()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_heartbeat_loss_cancels_handler_before_return(monkeypatch, unavailable) -> None:
    session, stopped = _session(), asyncio.Event()
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))
    pulse = (
        AsyncMock(side_effect=RuntimeError("database down")) if unavailable else AsyncMock(return_value=False)
    )
    monkeypatch.setattr(queue, "renew", pulse)

    async def handler(session, job):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(queue.LostClaim):
        await asyncio.wait_for(
            runner._with_heartbeat(handler, session, runner.QueueSpec({}, lease_seconds=1), _claimed()), 3
        )
    assert stopped.is_set()
    pulse.assert_awaited_once()


async def test_external_cancellation_waits_for_handler_cleanup(monkeypatch) -> None:
    session, entered, stopped = _session(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(runner, "get_sessionmaker", lambda: _maker(session))

    async def handler(session, job):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    work = asyncio.create_task(runner._with_heartbeat(handler, session, runner.QueueSpec({}), _claimed()))
    await entered.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert stopped.is_set()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_redis_lease_loss_stops_cooperative_work(monkeypatch, unavailable) -> None:
    @asynccontextmanager
    async def locked(name, ttl):
        yield "owner"

    fake = SimpleNamespace(
        eval=AsyncMock(side_effect=RuntimeError("redis down")) if unavailable else AsyncMock(return_value=0)
    )
    monkeypatch.setattr(kv, "lock", locked)
    monkeypatch.setattr(kv, "get_redis", lambda: fake)

    async def work():
        async with kv.lease("release-gate", ttl=3):
            await asyncio.Event().wait()

    with pytest.raises(kv.LockLost):
        await asyncio.wait_for(work(), 3)
    fake.eval.assert_awaited_once_with(kv._RENEW, 1, "lock:release-gate", "owner", 3)


async def test_redis_lease_remains_exclusive() -> None:
    async with kv.lease("test-release-gate", ttl=3):
        with pytest.raises(kv.LockNotAcquired):
            async with kv.lease("test-release-gate", ttl=3):
                pytest.fail("second owner entered")
    async with kv.lease("test-release-gate", ttl=3):
        pass  # owner-checked release freed the lock


async def test_service_predecessor_blocks_before_target_or_panel(monkeypatch) -> None:
    order = SimpleNamespace(id=32, kind=OrderKind.RENEW, service_id=9, status=OrderStatus.PAID, params={})
    session = SimpleNamespace(
        get=AsyncMock(return_value=order), scalar=AsyncMock(return_value=31), refresh=AsyncMock()
    )
    calls = []

    @asynccontextmanager
    async def lease(name, ttl):
        calls.append((name, ttl))
        yield "owner"

    inner = AsyncMock()
    monkeypatch.setattr(kv, "lease", lease)
    monkeypatch.setattr(lifecycle, "_fulfill", inner)
    with pytest.raises(lifecycle.ServiceBusy, match="31"):
        await lifecycle.fulfill(session, 32)
    assert calls == [("service:modify:9", 180)]
    inner.assert_not_awaited()


async def test_parked_filter_precedes_limit_and_cursor_advances(session) -> None:
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    rows = [
        Order(
            shop_id=shop.id,
            customer_id=customer.id,
            kind=OrderKind.RENEW,
            status=OrderStatus.PROVISIONING,
            params={"renewal": {"parked": i >= 201}},
        )
        for i in range(204)
    ]
    session.add_all(rows)
    await session.flush()
    first, ceiling = rows[0].id - 1, rows[-1].id
    # Eligible rows are beyond the old first-200 window.
    assert await lifecycle.parked_renewals(session, limit=2, after_id=first, through_id=ceiling) == [
        rows[201].id,
        rows[202].id,
    ]
    assert await lifecycle.parked_renewals(session, limit=2, after_id=rows[202].id, through_id=ceiling) == [
        rows[203].id
    ]
    assert await lifecycle.parked_renewals(session, after_id=ceiling, through_id=ceiling) == []


async def test_reconcile_pass_keeps_going_after_uncertain_rows(monkeypatch) -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=9))
    monkeypatch.setattr(jobs, "session_scope", _maker(session))
    pages = AsyncMock(side_effect=[[3, 4], [9]])
    reconciled = AsyncMock(return_value="uncertain")
    monkeypatch.setattr(lifecycle, "parked_renewals", pages)
    monkeypatch.setattr(lifecycle, "reconcile_renewal", reconciled)
    await jobs.reconcile_renewals()
    assert [call.kwargs for call in pages.await_args_list] == [
        {"after_id": 0, "through_id": 9},
        {"after_id": 4, "through_id": 9},
    ]
    assert [call.args[1] for call in reconciled.await_args_list] == [3, 4, 9]


async def _job(session):
    name = "release-gate-" + str(datetime.now(UTC).timestamp())
    job_id = await queue.enqueue(session, name, "test", {}, max_attempts=1)
    await session.commit()
    claimed = (await queue.claim(session, name, limit=1))[0]
    return claimed, await session.get(Job, job_id)


@pytest.mark.parametrize("expired", [False, True])
async def test_queue_fences_replaced_and_expired_claims(session, expired) -> None:
    claimed, row = await _job(session)
    if expired:
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    else:
        row.claim_token = "new-owner"
    await session.commit()
    assert not await queue.renew(session, claimed, lease_seconds=120)
    assert not await queue.complete(session, claimed)
    assert await queue.fail(session, claimed, "old failure", retry=False) is None
    assert not await queue.defer(session, claimed, "old deferral")
    with pytest.raises(queue.LostClaim):
        await queue.update_payload(session, claimed, {"stale": True})
    await session.refresh(row)
    assert row.status == JobStatus.RUNNING and not row.payload


async def test_defer_restores_attempt_and_leaves_retryable_job(session) -> None:
    claimed, row = await _job(session)
    assert claimed.attempts == claimed.max_attempts == 1
    assert await queue.defer(session, claimed, "predecessor", seconds=0)
    await session.commit()
    await session.refresh(row)
    assert row.status == JobStatus.QUEUED and row.attempts == 0
    assert row.finished_at is None and row.lease_until is None
    reclaimed = (await queue.claim(session, row.queue, limit=1))[0]
    assert reclaimed.attempts == 1 and reclaimed.token != claimed.token
    assert not await queue.complete(session, claimed)
    assert await queue.complete(session, reclaimed)
    await session.commit()
    assert await session.scalar(select(Job.status).where(Job.id == row.id)) == JobStatus.DONE


async def test_started_order_outranks_older_fresh_order_and_conflicts_are_quarantined(session) -> None:
    from paurubot.db.models import Service, ServiceStatus

    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="release-gate-ordering",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.flush()
    older = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        service_id=service.id,
        kind=OrderKind.EXTRA_DAYS,
        status=OrderStatus.PAID,
        params={},
    )
    started = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        service_id=service.id,
        kind=OrderKind.EXTRA_VOLUME,
        status=OrderStatus.PROVISIONING,
        params={"target": {"limit": 123}, "reset_protocol": 1},
    )
    session.add_all([older, started])
    await session.flush()
    assert older.id < started.id
    assert await lifecycle._modification_predecessor(session, older) == started.id
    assert await lifecycle._modification_predecessor(session, started) is None
    # A pre-deploy collision must not be resolved by letting either target overwrite the other.
    older.params = {"target": {"set_expire": True}}
    await session.flush()
    assert await lifecycle._modification_predecessor(session, older) == started.id
    assert await lifecycle._modification_predecessor(session, started) == older.id
    started.status = OrderStatus.COMPLETED
    await session.flush()
    assert await lifecycle._modification_predecessor(session, older) is None
