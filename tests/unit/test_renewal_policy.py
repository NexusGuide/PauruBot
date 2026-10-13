"""Renewal policy engine: explicit time/data rules, absolute targets and panel verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.db.models import Job, JobStatus, OrderKind, OrderStatus, PanelKind, Service, ServiceStatus
from paurubot.ledger import wallet
from paurubot.panels import GB, PanelUser, registry
from paurubot.provisioning import renewal_policy as rp
from paurubot.provisioning import sync
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _service(
    *,
    days_left: int | None = 10,
    limit_gb: int = 50,
    used_gb: int = 20,
    status: ServiceStatus = ServiceStatus.ACTIVE,
) -> Service:
    return Service(
        status=status,
        expire_at=NOW + timedelta(days=days_left) if days_left is not None else None,
        data_limit=limit_gb * GB,
        used_bytes=used_gb * GB,
    )


@pytest.mark.parametrize(
    ("mode", "expire_days", "limit_gb", "reset"),
    [
        ("extend_reset", 40, 30, True),  # time appended, data reset to the plan
        ("extend_carry", 40, 20 + 30 + 30, False),  # time appended, the 30 GB left carried over
        ("restart_reset", 30, 30, True),  # a fresh period with the plan's data
        ("restart_carry", 30, 30 + 30, True),  # a fresh period, leftover data on top
    ],
)
def test_renew_modes_are_independent_time_and_data_rules(mode, expire_days, limit_gb, reset) -> None:  # type: ignore[no-untyped-def]
    policy = rp.policy_for(OrderKind.RENEW, mode)
    target = rp.compute(_service(), policy, days=30, data_bytes=30 * GB, now=NOW)
    assert target.expire == NOW + timedelta(days=expire_days)
    assert target.limit == limit_gb * GB
    assert target.reset is reset
    assert target.set_expire


def test_lapsed_service_always_starts_fresh() -> None:
    service = _service(days_left=-3, status=ServiceStatus.EXPIRED)
    policy = rp.policy_for(OrderKind.RENEW, "extend_carry", lapsed=service.status in rp.LAPSED)
    assert policy == rp.FRESH
    target = rp.compute(service, policy, days=30, data_bytes=30 * GB, now=NOW)
    assert target.expire == NOW + timedelta(days=30) and target.limit == 30 * GB and target.reset


def test_queued_renewal_never_carries_data() -> None:
    assert rp.policy_for(OrderKind.RENEW, "extend_carry", queued=True) == rp.MODE_POLICIES["extend_reset"]
    assert rp.policy_for(OrderKind.RENEW, "restart_reset", queued=True) == rp.MODE_POLICIES["restart_reset"]


def test_unlimited_plan_and_addons() -> None:
    unlimited = rp.compute(_service(), rp.policy_for(OrderKind.RENEW), days=0, data_bytes=0, now=NOW)
    assert unlimited.set_expire and unlimited.expire is None and unlimited.limit == 0

    extra_gb = rp.compute(
        _service(), rp.policy_for(OrderKind.EXTRA_VOLUME), days=0, data_bytes=10 * GB, addon=True, now=NOW
    )
    assert not extra_gb.set_expire and extra_gb.limit == 60 * GB and not extra_gb.reset

    extra_days = rp.compute(
        _service(days_left=-1), rp.policy_for(OrderKind.EXTRA_DAYS), days=5, data_bytes=0, addon=True, now=NOW
    )
    assert extra_days.expire == NOW + timedelta(days=5) and extra_days.limit is None

    # Add-ons never limit what is unlimited.
    endless = _service(days_left=None, limit_gb=0)
    assert not rp.compute(
        endless, rp.policy_for(OrderKind.EXTRA_DAYS), days=5, data_bytes=0, addon=True
    ).set_expire
    assert (
        rp.compute(endless, rp.policy_for(OrderKind.EXTRA_VOLUME), days=0, data_bytes=GB, addon=True).limit
        == 0
    )

    with pytest.raises(rp.UnsupportedChange):
        rp.policy_for(OrderKind.PURCHASE)


def test_target_params_keep_the_before_state() -> None:
    params = rp.compute(
        _service(), rp.policy_for(OrderKind.UPGRADE), days=60, data_bytes=200 * GB, now=NOW
    ).as_params()
    assert params["policy"] == {"time": "reset", "volume": "reset", "reset_usage": True}
    assert params["before"] == {
        "expire": (NOW + timedelta(days=10)).isoformat(),
        "limit": 50 * GB,
        "used": 20 * GB,
        "status": "active",
    }
    assert params["expire"] == (NOW + timedelta(days=60)).isoformat() and params["limit"] == 200 * GB


def test_verify_detects_changes_the_panel_did_not_keep() -> None:
    params = rp.compute(
        _service(), rp.policy_for(OrderKind.RENEW), days=30, data_bytes=30 * GB, now=NOW
    ).as_params()
    good = PanelUser("u", used_bytes=0, data_limit=30 * GB, expire_at=NOW + timedelta(days=40, hours=3))
    assert rp.verify(params, good) == []
    stale = PanelUser(
        "u", status="disabled", used_bytes=20 * GB, data_limit=50 * GB, expire_at=NOW + timedelta(days=10)
    )
    assert rp.verify(params, stale) == [
        "still_disabled",
        "expire_mismatch",
        "limit_mismatch",
        "usage_not_reset",
    ]
    # Panels that cannot report limits are trusted for them.
    blind = PanelUser("u", used_bytes=0, limits_known=False)
    assert rp.verify(params, blind) == []


# --- through the provisioning queue ---------------------------------------------------------


async def _drain(session) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, "provision", limit=1)
        if not claimed:
            return total
        await run_job("provision", QUEUES["provision"], claimed[0])
        total += 1


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


async def test_renewal_is_verified_on_the_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    await _drain(session)
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    env["fake"].users[service.panel_username]["used_traffic"] = 10 * GB
    await sync.sync_panel(session, env["panel"])

    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED
    assert renewal.params["verification"]["ok"] is True
    assert renewal.params["target"]["before"]["used"] == 10 * GB
    assert renewal.params["target"]["policy"] == {"time": "append", "volume": "reset", "reset_usage": True}
    assert env["fake"].users[service.panel_username]["used_traffic"] == 0


async def test_unconfirmed_renewal_is_retried_without_a_second_reset(session, env) -> None:  # type: ignore[no-untyped-def]
    fake, panel = env["fake"], env["panel"]
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    await _drain(session)
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    name = service.panel_username
    fake.users[name]["used_traffic"] = 10 * GB
    await sync.sync_panel(session, panel)
    original = fake.handler

    def ignore_updates(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == f"/api/user/{name}":
            return httpx.Response(200, json=fake._user_out(fake.users[name]))  # accepted, not applied
        return original(request)

    registry.test_transports[panel.id] = httpx.MockTransport(ignore_updates)
    registry._cache.clear()
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.PROVISIONING  # not completed, not refunded
    assert renewal.params["verification"]["ok"] is False
    assert "expire_mismatch" in renewal.params["verification"]["problems"]
    assert renewal.params["reset_done"] is True

    # Traffic used after the reset must survive the retry.
    fake.users[name]["used_traffic"] = 200 * 1024**2
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{renewal.id}:provision"))
    job.run_at = datetime.now(UTC)
    await session.commit()
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED
    assert renewal.params["verification"]["ok"] is True
    assert fake.users[name]["used_traffic"] == 200 * 1024**2
    renewed = await session.get(Service, service.id, populate_existing=True)
    assert renewed.used_bytes == 200 * 1024**2
    assert renewed.data_limit == 50 * GB
