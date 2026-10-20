from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from paurubot.core import orders, trials
from paurubot.db.models import (
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelKind,
    Service,
    ServiceStatus,
    TrialGrant,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from paurubot.provisioning import sync
from paurubot.security.secretbox import encrypt
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeMarzneshinPanel, FakeTokenPanel, FakeXuiPanel, transport


async def drain(session, name: str) -> int:  # type: ignore[no-untyped-def]
    """Run due jobs one at a time (the test connection cannot run statements concurrently)."""
    total = 0
    while True:
        claimed = await queue.claim(session, name, limit=1)
        if not claimed:
            return total
        await run_job(name, QUEUES[name], claimed[0])
        total += 1


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    """Other test modules commit real jobs; park them so each test only drains its own."""
    from sqlalchemy import update

    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
def fakes():  # type: ignore[no-untyped-def]
    installed: dict[int, object] = {}
    yield installed
    registry.test_transports.clear()
    registry._cache.clear()


async def _setup(session, fakes, kind: PanelKind = PanelKind.MARZBAN, **options):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    if kind == PanelKind.XUI:
        options.setdefault("inbound_id", 3)
        options.setdefault("sub_base_url", "https://sub.example.com/sub/")
    panel = await factories.panel(session, shop, kind, **options)
    if kind == PanelKind.XUI:
        fake: Any = FakeXuiPanel()
    elif kind == PanelKind.MARZNESHIN:
        fake = FakeMarzneshinPanel()
    else:
        fake = FakeTokenPanel(kind.value)
    registry.test_transports[panel.id] = transport(fake)
    fakes[panel.id] = fake
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
    return shop, panel, plan, cust, fake


async def _buy(session, cust, kind, **kw):  # type: ignore[no-untyped-def]
    order = await orders.create(session, cust, await orders.draft(session, cust, kind, **kw))
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


@pytest.mark.parametrize(
    "kind", [PanelKind.MARZBAN, PanelKind.PASARGUARD, PanelKind.XUI, PanelKind.MARZNESHIN]
)
async def test_purchase_renew_extras_upgrade(session, fakes, kind) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes, kind)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    assert await drain(session, "provision") == 1
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    service = await session.get(Service, order.service_id)
    assert service is not None and service.status == ServiceStatus.ACTIVE
    assert service.data_limit == 50 * GB and service.subscription_url
    first_expire = service.expire_at
    assert first_expire is not None

    await _buy(session, cust, OrderKind.RENEW, plan=plan, service=service)
    await drain(session, "provision")
    await session.refresh(service)
    assert service.expire_at is not None and service.expire_at - first_expire >= timedelta(days=29)

    await _buy(session, cust, OrderKind.EXTRA_VOLUME, service=service, extra_gb=10)
    await drain(session, "provision")
    await session.refresh(service)
    assert service.data_limit == 60 * GB

    big = await factories.plan(session, shop, panel, price=300_000, days=60, data_gb=200, title="Big")
    await _buy(session, cust, OrderKind.UPGRADE, plan=big, service=service)
    await drain(session, "provision")
    await session.refresh(service)
    assert service.data_limit == 200 * GB and service.plan_id == big.id
    notify_jobs = (await session.scalars(select(Job).where(Job.queue == "notify"))).all()
    assert any(j.kind == "order_completed" for j in notify_jobs)


async def test_retry_adopts_account_created_before_a_crash(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    # First attempt: the panel creates the user but the response is lost.
    original = fake.handler

    def lose_response(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST" and request.url.path == "/api/user":
            return httpx.Response(502, text="bad gateway")
        return response

    registry.test_transports[panel.id] = httpx.MockTransport(lose_response)
    registry._cache.clear()
    claimed = await queue.claim(session, "provision", limit=1)
    await run_job("provision", QUEUES["provision"], claimed[0])
    assert len(fake.users) == 1
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{order.id}:provision"))
    job.run_at = datetime.now(UTC)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    assert len(fake.users) == 1  # adopted, not duplicated


async def test_final_failure_refunds_and_releases_trial(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes)
    before = await wallet_balance(session, shop.id, cust.id)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    fake.password = None
    panel.password_enc = encrypt("panel", "wrong-password")
    await session.commit()
    registry._cache.clear()
    for _ in range(10):
        job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{order.id}:provision"))
        if job.status == JobStatus.DEAD:
            break
        job.run_at = datetime.now(UTC)
        await session.commit()
        await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
    assert await wallet_balance(session, shop.id, cust.id) == before

    trial_cust = await factories.customer(session, shop)
    trial_order = await trials.request(session, trial_cust)
    await session.commit()
    for _ in range(6):
        job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{trial_order.id}:provision"))
        if job.status == JobStatus.DEAD:
            break
        job.run_at = datetime.now(UTC)
        await session.commit()
        await drain(session, "provision")
    grant = await session.scalar(select(TrialGrant).where(TrialGrant.customer_id == trial_cust.id))
    assert grant is None  # released, the customer may try again


async def test_trial_provisioning(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes)
    trial_cust = await factories.customer(session, shop)
    order = await trials.request(session, trial_cust)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None and service.is_trial and service.data_limit == GB
    grant = await session.scalar(select(TrialGrant).where(TrialGrant.customer_id == trial_cust.id))
    assert grant.status == "issued" and grant.service_id == service.id


async def test_sync_alerts_once_and_detects_deleted(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session, fakes)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    fake.users[service.panel_username]["used_traffic"] = 48 * GB
    synced, failed = await sync.sync_panel(session, panel)
    assert (synced, failed) == (1, 0)
    await sync.sync_panel(session, panel)
    alerts = (await session.scalars(select(Job).where(Job.kind == "service_alert"))).all()
    assert [j.payload["alert"] for j in alerts] == ["low_volume"]
    fake.users[service.panel_username]["used_traffic"] = 50 * GB
    await sync.sync_panel(session, panel)
    await session.refresh(service)
    assert service.status == ServiceStatus.LIMITED
    del fake.users[service.panel_username]
    await sync.sync_panel(session, panel)
    await session.refresh(service)
    assert service.status == ServiceStatus.DELETED


async def test_panel_webhook_requires_secret_and_queues_authoritative_refresh(session, fakes) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app

    shop, panel, plan, cust, fake = await _setup(session, fakes)
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    panel.webhook_secret_enc = encrypt("webhook", "hook-secret-123")
    await session.commit()
    event = [
        {
            "action": "user_limited",
            "username": service.panel_username,
            "user": {
                "username": service.panel_username,
                "status": "limited",
                "used_traffic": 50 * GB,
                "data_limit": 50 * GB,
                "expire": 0,
            },
        }
    ]
    event += [event[0], {"user": {"username": 123}}]
    app_transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=app_transport, base_url="http://t") as client:
        bad = await client.post(
            f"/api/webhooks/panel/{panel.id}", content=json.dumps(event), headers={"x-webhook-secret": "nope"}
        )
        assert bad.status_code == 403
        ok = await client.post(
            f"/api/webhooks/panel/{panel.id}",
            content=json.dumps(event),
            headers={"x-webhook-secret": "hook-secret-123"},
        )
        assert ok.status_code == 200 and ok.json() == {"received": 3, "applied": 0, "queued": 1}
    await session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE
    fake.users[service.panel_username].update(status="limited", used_traffic=50 * GB)
    assert await drain(session, "provision") == 1
    await session.refresh(service)
    assert service.status == ServiceStatus.LIMITED


async def test_marzneshin_driver_details(session, fakes) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels import PanelRef, UserSpec

    shop, panel, plan, cust, fake = await _setup(session, fakes, PanelKind.MARZNESHIN, service_ids=[2])
    driver = await registry.driver_for(panel)
    assert [o.id for o in await driver.list_options()] == ["1", "2"]
    held = await driver.create_user(UserSpec("held_1", data_limit=GB, on_hold_days=10))
    assert held.status == "on_hold" and held.expire_at is None
    created = fake.users["held_1"]
    assert created["service_ids"] == [2] and created["usage_duration"] == 10 * 86400
    off = await driver.update_user(PanelRef("held_1"), enabled=False)
    assert off.status == "disabled"
    later = datetime.now(UTC) + timedelta(days=5)
    on = await driver.update_user(PanelRef("held_1"), enabled=True, expire_at=later, data_limit=2 * GB)
    assert on.status == "active" and on.data_limit == 2 * GB and on.expire_at is not None
    users, total = await driver.list_users(0, 100)
    assert total == 1 and users[0].username == "held_1"
    assert await driver.get_user(PanelRef("missing")) is None
