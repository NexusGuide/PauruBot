"""SEC-F09: background sync and cleanup never overwrite or delete a service mid-change.

These tests use real, separate PostgreSQL connections: the races are between transactions.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot import kv
from paurubot.core import settings as shop_settings
from paurubot.db.models import Order, OrderKind, OrderStatus, Panel, PanelKind, Service, ServiceStatus, Shop
from paurubot.panels import GB, registry
from paurubot.provisioning import lifecycle, sync
from paurubot.worker import jobs as worker_jobs
from paurubot.worker import queue
from paurubot.worker.runner import Defer
from tests import factories
from tests.fakes.panels import FakeTokenPanel


@pytest.fixture(autouse=True)
def _fresh_drivers():  # type: ignore[no-untyped-def]
    registry._cache.clear()
    yield
    registry.test_transports.clear()
    registry._cache.clear()


def _user(fake: FakeTokenPanel, name: str, expire: datetime, limit: int, used: int = 0) -> None:
    fake.users[name] = {
        "id": len(fake.users) + 1,
        "username": name,
        "status": "active",
        "used_traffic": used,
        "data_limit": limit,
        "expire": int(expire.timestamp()),
        "sub": secrets.token_hex(6),
        "request": {},
    }


async def _world(maker, count: int = 1, kind: PanelKind = PanelKind.MARZBAN):  # type: ignore[no-untyped-def]
    """Committed shop, panel, customer and `count` services that exist on the fake panel."""
    fake = FakeTokenPanel(kind.value)
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=10)
    async with maker() as s:
        shop = await factories.shop(s, "Ownership " + secrets.token_hex(3))
        panel = await factories.panel(s, shop, kind)
        cust = await factories.customer(s, shop)
        ids = []
        for i in range(count):
            name = f"own_{secrets.token_hex(4)}_{i}"
            _user(fake, name, expire, 50 * GB)
            service = Service(
                shop_id=shop.id,
                customer_id=cust.id,
                panel_id=panel.id,
                panel_username=name,
                status=ServiceStatus.ACTIVE,
                data_limit=50 * GB,
                expire_at=expire,
                sub_token=secrets.token_urlsafe(12),
            )
            s.add(service)
            await s.flush()
            ids.append(service.id)
        await s.commit()
        return shop.id, panel.id, cust.id, ids, fake, expire


async def test_sync_never_writes_a_stale_panel_read_over_a_newer_renewal(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, panel_id, _, (service_id,), fake, old_expire = await _world(maker)
    reached, release = asyncio.Event(), asyncio.Event()

    async def slow_read(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/api/user/"):
            response = fake.handler(request)  # the panel answers with the pre-renewal state
            reached.set()
            await release.wait()
            return response
        return fake.handler(request)

    registry.test_transports[panel_id] = httpx.MockTransport(slow_read)
    new_expire = old_expire + timedelta(days=30)

    async def run_sync() -> None:
        async with maker() as s:
            panel = await s.get(Panel, panel_id)
            assert panel is not None
            await sync.sync_panel(s, panel)

    async def renewal_write() -> None:
        # What a renewal commits after the panel accepted it: the newer, absolute target.
        async with maker() as s:
            await s.execute(
                update(Service)
                .where(Service.id == service_id)
                .values(expire_at=new_expire, data_limit=100 * GB)
            )
            await s.commit()

    syncing = asyncio.create_task(run_sync())
    await asyncio.wait_for(reached.wait(), 5)
    writing = asyncio.create_task(renewal_write())
    await asyncio.sleep(0.3)
    # The sync owns the row from its panel read to its commit, so the renewal waits.
    assert not writing.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(syncing, writing), 10)
    async with maker() as s:
        row = await s.get(Service, service_id)
        assert row is not None
        assert row.expire_at == new_expire and row.data_limit == 100 * GB


async def test_sync_does_not_delete_a_service_moved_away_mid_pass(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, panel_id, _, (first, second), fake, _ = await _world(maker, count=2)
    async with maker() as s:
        shop = await s.get(Shop, shop_id)
        assert shop is not None
        other_id = (await factories.panel(s, shop)).id
        names = {
            row.id: row.panel_username
            for row in await s.scalars(select(Service).where(Service.id.in_([first, second])))
        }
        await s.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/api/user/{names[first]}":
            # While the sync reads the first service, a move of the second one finishes:
            # it now lives on another panel and its old account here is gone.
            async with maker() as s:
                await s.execute(update(Service).where(Service.id == second).values(panel_id=other_id))
                await s.commit()
            fake.users.pop(names[second], None)
        return fake.handler(request)

    registry.test_transports[panel_id] = httpx.MockTransport(handler)
    async with maker() as s:
        panel = await s.get(Panel, panel_id)
        assert panel is not None
        await sync.sync_panel(s, panel)
    async with maker() as s:
        row = await s.get(Service, second)
        assert row is not None and row.panel_id == other_id
        assert row.status == ServiceStatus.ACTIVE  # not "missing on panel" from a stale copy


async def test_sync_skips_a_service_owned_by_a_modification(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    _, panel_id, _, (service_id,), fake, expire = await _world(maker)
    fake.users[next(iter(fake.users))]["expire"] = 0  # the panel would report "no expiry"
    registry.test_transports[panel_id] = httpx.MockTransport(fake.handler)
    async with kv.lease(lifecycle.service_lease(service_id), ttl=30):
        async with maker() as s:
            panel = await s.get(Panel, panel_id)
            assert panel is not None
            assert await sync.sync_panel(s, panel) == (0, 0)
    async with maker() as s:
        row = await s.get(Service, service_id)
        assert row is not None and row.expire_at == expire  # untouched while owned


async def _expired_world(maker):  # type: ignore[no-untyped-def]
    shop_id, panel_id, cust_id, (service_id,), fake, _ = await _world(maker)
    long_ago = datetime.now(UTC) - timedelta(days=40)
    async with maker() as s:
        await shop_settings.set_many(s, shop_id, {"delete_expired_after_days": 7})
        await s.execute(
            update(Service)
            .where(Service.id == service_id)
            .values(status=ServiceStatus.EXPIRED, expire_at=long_ago)
        )
        await s.commit()
    registry.test_transports[panel_id] = httpx.MockTransport(fake.handler)
    return shop_id, panel_id, cust_id, service_id, fake


async def test_cleanup_keeps_a_service_with_a_paid_renewal(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, _, cust_id, service_id, fake = await _expired_world(maker)
    async with maker() as s:
        s.add(
            Order(
                shop_id=shop_id,
                customer_id=cust_id,
                kind=OrderKind.RENEW,
                status=OrderStatus.PAID,
                amount=100_000,
                service_id=service_id,
                params={},
            )
        )
        await s.commit()
    async with maker() as s:
        assert await sync.cleanup_expired(s, shop_id) == 0
    async with maker() as s:
        row = await s.get(Service, service_id)
        assert row is not None and row.status == ServiceStatus.EXPIRED
    assert len(fake.users) == 1  # the panel account the renewal will extend still exists


async def test_cleanup_skips_a_service_being_changed_and_deletes_it_later(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, _, _, service_id, fake = await _expired_world(maker)
    async with kv.lease(lifecycle.service_lease(service_id), ttl=30):
        async with maker() as s:
            assert await sync.cleanup_expired(s, shop_id) == 0
    assert len(fake.users) == 1
    async with maker() as s:
        assert await sync.cleanup_expired(s, shop_id) == 1
    async with maker() as s:
        row = await s.get(Service, service_id)
        assert row is not None and row.status == ServiceStatus.DELETED
    assert fake.users == {}


async def test_cleanup_decides_again_on_the_locked_current_row(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, _, _, service_id, fake = await _expired_world(maker)
    original = sync._cleanup_one

    async def renewed_meanwhile(session, shop, sid, cutoff):  # type: ignore[no-untyped-def]
        async with maker() as other:
            await other.execute(
                update(Service)
                .where(Service.id == sid)
                .values(status=ServiceStatus.ACTIVE, expire_at=datetime.now(UTC) + timedelta(days=30))
            )
            await other.commit()
        return await original(session, shop, sid, cutoff)

    sync._cleanup_one = renewed_meanwhile  # type: ignore[assignment]
    try:
        async with maker() as s:
            assert await sync.cleanup_expired(s, shop_id) == 0
    finally:
        sync._cleanup_one = original  # type: ignore[assignment]
    assert len(fake.users) == 1


async def test_webhook_refresh_defers_while_a_modification_owns_the_service(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    shop_id, panel_id, _, (service_id,), fake, _ = await _world(maker)
    registry.test_transports[panel_id] = httpx.MockTransport(fake.handler)
    job = queue.Claimed(
        id=0,
        kind="refresh_service",
        payload={"service_id": service_id, "panel_id": panel_id},
        attempts=1,
        max_attempts=8,
        shop_id=shop_id,
        token="t",
    )
    async with kv.lease(lifecycle.service_lease(service_id), ttl=30):
        async with maker() as s:
            with pytest.raises(Defer):
                await worker_jobs.refresh_panel_hint(s, job)
    async with maker() as s:
        await worker_jobs.refresh_panel_hint(s, job)
        row = await s.scalar(select(Service).where(Service.id == service_id))
        assert row is not None and row.synced_at is not None
