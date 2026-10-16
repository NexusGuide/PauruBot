"""Renewals survive a worker that dies mid-change, and panels that cannot prove a change (F-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.db.models import Job, JobStatus, OrderKind, OrderStatus, PanelKind, Service
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, PanelUser, registry
from paurubot.provisioning import lifecycle, sync
from paurubot.provisioning import renewal_state as rs
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


class WorkerKilled(BaseException):
    """Stands in for the worker process dying mid-call (SIGKILL, OOM, host restart).

    A BaseException skips every ``except Exception``, so nothing records the failure: the job
    stays leased and the order keeps whatever state was committed last, as after a real crash.
    """


async def _drain(session) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, "provision", limit=1)
        if not claimed:
            return total
        await run_job("provision", QUEUES["provision"], claimed[0])
        total += 1


async def _job(session, order_id: int) -> Job:  # type: ignore[no-untyped-def]
    job = await session.scalar(
        select(Job)
        .where(Job.dedupe_key == f"order:{order_id}:provision")
        .execution_options(populate_existing=True)
    )
    assert job is not None
    return job


async def _restart_worker(session, order_id: int) -> None:  # type: ignore[no-untyped-def]
    """A new worker takes over the dead one's job once its lease runs out."""
    job = await _job(session, order_id)
    assert job.status == JobStatus.RUNNING  # the dead worker never released it
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await _drain(session)


async def _run_until_dead(session, order_id: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(40):
        job = await _job(session, order_id)
        if job.status in {JobStatus.DEAD, JobStatus.DONE}:
            return
        job.run_at = datetime.now(UTC)
        await session.commit()
        await _drain(session)
    raise AssertionError("job never finished")


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 2_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "cust": cust, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _buy(session, cust, kind, **kw):  # type: ignore[no-untyped-def]
    from paurubot.core import orders

    order = await orders.create(session, cust, await orders.draft(session, cust, kind, **kw))
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


async def _service(session, env) -> Service:  # type: ignore[no-untyped-def]
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    await _drain(session)
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None
    return service


def _hook(env, name: str, *, crash: bool = False, apply: bool = True, status: int = 500, times: int = 1):  # type: ignore[no-untyped-def]
    """Break the first `times` user updates: kill the worker, or answer with an error.

    `apply` decides whether the panel kept the change before that happened. Later updates work.
    """
    fake, calls = env["fake"], {"put": 0}
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == f"/api/user/{name}":
            calls["put"] += 1
            if calls["put"] <= times:
                if apply:
                    original(request)
                if crash:
                    raise WorkerKilled
                return httpx.Response(status, json={"detail": "boom"})
        return original(request)

    registry.test_transports[env["panel"].id] = httpx.MockTransport(handler)
    registry._cache.clear()
    return calls


# --- worker restart ---------------------------------------------------------------------------


async def test_worker_dies_after_the_panel_applied_it(session, env) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    calls = _hook(env, service.panel_username, crash=True, apply=True)
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)

    with pytest.raises(WorkerKilled):
        await _drain(session)
    await session.refresh(renewal)
    assert rs.state(renewal) == rs.RUNNING and renewal.status == OrderStatus.PROVISIONING

    await _restart_worker(session, renewal.id)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED
    assert rs.state(renewal) == rs.RECONCILED
    notes = [h.get("note") for h in rs.record(renewal)["history"]]
    assert "interrupted" in notes
    assert calls["put"] == 1  # found on the panel: not applied a second time
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged  # charged once


async def test_worker_dies_before_the_panel_applied_it(session, env) -> None:  # type: ignore[no-untyped-def]
    fake, panel = env["fake"], env["panel"]
    service = await _service(session, env)
    name = service.panel_username
    fake.users[name]["used_traffic"] = 10 * GB
    await sync.sync_panel(session, panel)
    calls = _hook(env, name, crash=True, apply=False)
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)

    with pytest.raises(WorkerKilled):
        await _drain(session)
    await session.refresh(renewal)
    assert rs.state(renewal) == rs.RUNNING and renewal.params["reset_done"] is True

    # Traffic the customer uses while the worker is down must survive the restart.
    fake.users[name]["used_traffic"] = 200 * 1024**2
    await _restart_worker(session, renewal.id)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED
    assert rs.state(renewal) == rs.APPLIED
    assert rs.record(renewal)["attempts"] == 2
    assert calls["put"] == 2  # the panel did not have it, so it was applied once more
    assert fake.users[name]["used_traffic"] == 200 * 1024**2  # no second usage reset
    renewed = await session.get(Service, service.id, populate_existing=True)
    assert renewed.data_limit == 50 * GB
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged


# --- panels that cannot report limits -----------------------------------------------------------


class _Blind:
    """A panel driver that only knows usage (PNL-11): its account proves nothing about limits."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def get_user(self, ref):  # type: ignore[no-untyped-def]
        return PanelUser(self.name, used_bytes=0, limits_known=False)


async def _parked(session, env, service: Service):  # type: ignore[no-untyped-def]
    env["plan"].renew_mode = "extend_carry"
    await session.commit()
    _hook(env, service.panel_username, apply=False, status=500, times=10_000)  # the panel refuses
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)
    await _run_until_dead(session, renewal.id)
    await session.refresh(renewal)
    assert rs.parked(renewal) and rs.state(renewal) == rs.UNCERTAIN
    return renewal, charged


async def test_blind_panel_is_never_reconciled_from_what_it_shows(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    renewal, charged = await _parked(session, env, service)

    async def blind_driver(panel):  # type: ignore[no-untyped-def]
        return _Blind(service.panel_username)

    monkeypatch.setattr(lifecycle, "driver_for", blind_driver)
    # Before this fix a blind panel always looked "applied" and the order completed unchecked.
    assert await lifecycle.reconcile_renewal(session, renewal.id) == "reapplied"
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.PROVISIONING and not rs.parked(renewal)
    assert (await _job(session, renewal.id)).status == JobStatus.QUEUED
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged

    # The job runs again against the panel, which now takes the change.
    monkeypatch.undo()
    registry.test_transports[env["panel"].id] = transport(env["fake"])
    registry._cache.clear()
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED and rs.state(renewal) == rs.APPLIED
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged


async def test_blind_panel_goes_to_staff_after_a_few_tries(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    renewal, charged = await _parked(session, env, service)
    renewal.params = {
        **renewal.params,
        "renewal": {**rs.record(renewal), "checks": lifecycle.UNVERIFIABLE_RETRIES},
    }
    await session.commit()
    alerts: list[str] = []

    async def blind_driver(panel):  # type: ignore[no-untyped-def]
        return _Blind(service.panel_username)

    async def alert(session, order, outcome, detail=None):  # type: ignore[no-untyped-def]
        alerts.append(outcome)

    monkeypatch.setattr(lifecycle, "driver_for", blind_driver)
    monkeypatch.setattr(lifecycle, "_alert_uncertain", alert)
    assert await lifecycle.reconcile_renewal(session, renewal.id) == "uncertain"
    assert await lifecycle.reconcile_renewal(session, renewal.id) == "uncertain"
    assert alerts == ["unverifiable"]  # told once
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.PROVISIONING and rs.parked(renewal)
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged  # no refund


@pytest.mark.parametrize("used_before", [0, 10 * GB])
async def test_lost_reset_response_never_resets_twice(session, env, used_before) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    fake, panel, name = env["fake"], env["panel"], service.panel_username
    fake.users[name]["used_traffic"] = used_before
    await sync.sync_panel(session, panel)
    original, calls = fake.handler, {"reset": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/api/user/{name}/reset":
            calls["reset"] += 1
            original(request)  # the remote operation succeeds, but its response is lost
            raise httpx.ReadTimeout("lost reset response", request=request)
        return original(request)

    registry.test_transports[panel.id] = httpx.MockTransport(handler)
    registry._cache.clear()
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.params["reset_attempted"]
    assert not renewal.params.get("reset_done")
    assert rs.state(renewal) == rs.UNCERTAIN

    # New customer traffic must not be erased by a retry.
    fake.users[name]["used_traffic"] = 200 * 1024**2
    await _run_until_dead(session, renewal.id)
    await session.refresh(renewal)
    assert calls["reset"] == 1
    assert fake.users[name]["used_traffic"] == 200 * 1024**2
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged
    if used_before:
        assert renewal.status == OrderStatus.COMPLETED
    else:
        # The old counter was too small to prove a reset: keep it for staff, never refund blindly.
        assert renewal.status == OrderStatus.PROVISIONING and rs.parked(renewal)
        assert await lifecycle.reconcile_renewal(session, renewal.id) == "uncertain"
        await session.refresh(renewal)
        assert renewal.status == OrderStatus.PROVISIONING
        assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged
