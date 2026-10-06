"""Bulk service actions (SVC-07, ADM-04) and list sorting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import AuditLog, Job, JobStatus, OrderKind, Service, StaffRole
from paurubot.panels import GB
from tests import factories
from tests.unit.test_provisioning import _buy, _setup, drain

PASSWORD = "a-very-strong-pass"


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    from sqlalchemy import update

    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
def fakes():  # type: ignore[no-untyped-def]
    from paurubot.panels import registry

    installed: dict[int, object] = {}
    yield installed
    registry.test_transports.clear()
    registry._cache.clear()


@asynccontextmanager
async def _owner(session, shop) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:  # type: ignore[no-untyped-def]
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="bulk_owner", password=PASSWORD)
    await session.commit()
    transport = httpx.ASGITransport(app=create_app(), client=("10.1.1.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/auth/login", json={"username": "bulk_owner", "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        yield client, resp.json()["csrf_token"]


async def test_bulk_extend_by_filter_queues_and_applies(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, _fake = await _setup(session, fakes)
    for _ in range(3):
        await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    assert await drain(session, "provision") == 3
    services = list(await session.scalars(select(Service).where(Service.shop_id == shop.id)))
    before = {s.id: (s.expire_at, s.data_limit) for s in services}

    async with _owner(session, shop) as (client, csrf):
        resp = await client.post(
            "/api/admin/services/bulk",
            json={"action": "extend", "filter": {"panel_id": panel.id}, "days": 3, "gb": 5},
            headers={"x-csrf-token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["queued"] == 3
        assert await drain(session, "bulk") == 3
        for service in services:
            await session.refresh(service)
            old_expire, old_limit = before[service.id]
            assert old_expire is not None and service.expire_at is not None
            assert service.expire_at - old_expire == timedelta(days=3)
            assert service.data_limit == old_limit + 5 * GB
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.shop_id == shop.id, AuditLog.action == "service.bulk_extend")
        )
        assert audit is not None and audit.after["count"] == 3

        listed = await client.get("/api/admin/services", params={"sort": "-expire_at"})
        assert listed.status_code == 200
        bad = await client.get("/api/admin/services", params={"sort": "password"})
        assert bad.status_code == 422


async def test_bulk_needs_ids_or_filter_and_scopes_to_shop(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, _fake = await _setup(session, fakes)
    other = await factories.shop(session, "Elsewhere")
    other_panel = await factories.panel(session, other)
    other_cust = await factories.customer(session, other)
    foreign = Service(
        shop_id=other.id, customer_id=other_cust.id, panel_id=other_panel.id, panel_username="x"
    )
    session.add(foreign)
    await session.flush()
    async with _owner(session, shop) as (client, csrf):
        headers = {"x-csrf-token": csrf}
        neither = await client.post("/api/admin/services/bulk", json={"action": "disable"}, headers=headers)
        assert neither.status_code == 422
        resp = await client.post(
            "/api/admin/services/bulk", json={"action": "disable", "ids": [foreign.id]}, headers=headers
        )
        assert resp.status_code == 200 and resp.json()["queued"] == 0
        empty = await client.post(
            "/api/admin/services/bulk", json={"action": "extend", "ids": [1]}, headers=headers
        )
        assert empty.status_code == 422 and empty.json()["error"] == "nothing_to_extend"


async def test_bulk_customer_tags_and_tag_filter(session, fakes) -> None:  # type: ignore[no-untyped-def]
    shop, _panel, _plan, cust, _fake = await _setup(session, fakes)
    second = await factories.customer(session, shop, username="second")
    async with _owner(session, shop) as (client, csrf):
        resp = await client.post(
            "/api/admin/customers/bulk",
            json={"action": "tag", "ids": [cust.id, second.id], "tag": "vip"},
            headers={"x-csrf-token": csrf},
        )
        assert resp.status_code == 200 and resp.json()["updated"] == 2
        listed = (await client.get("/api/admin/customers", params={"tag": "vip"})).json()
        assert {c["id"] for c in listed["items"]} == {cust.id, second.id}
        await client.post(
            "/api/admin/customers/bulk",
            json={"action": "untag", "ids": [second.id], "tag": "vip"},
            headers={"x-csrf-token": csrf},
        )
        listed = (await client.get("/api/admin/customers", params={"tag": "vip"})).json()
        assert [c["id"] for c in listed["items"]] == [cust.id]
