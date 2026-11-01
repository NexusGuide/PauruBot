"""SEC-F27: usage sync runs panels side by side, each worker in its own session.

Before, `usage.sync` refreshed every service of every panel one after another: a slow panel
(15 s timeouts) held up all the others, and a large install could not finish a pass inside its
interval. Concurrency must not share an AsyncSession between tasks, and must not race
cookie logins of panels that would lock the admin out.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.db import session as db_session
from paurubot.db.models import Panel, PanelKind, Service, ServiceStatus
from paurubot.panels import GB, registry
from paurubot.provisioning import sync
from tests import factories
from tests.fakes.panels import FakeTokenPanel


@pytest.fixture
async def maker(engine):  # type: ignore[no-untyped-def]
    real = async_sessionmaker(engine, expire_on_commit=False)
    db_session.set_sessionmaker(real)
    registry._cache.clear()
    yield real
    registry.test_transports.clear()
    registry._cache.clear()
    db_session.set_sessionmaker(None)  # type: ignore[arg-type]


class Probe:
    """A fake panel whose every request takes `delay` seconds; records the peak in flight."""

    def __init__(self, delay: float, status: int | None = None) -> None:
        self.fake = FakeTokenPanel("marzban")
        self.delay, self.status = delay, status
        self.inflight = self.peak = self.calls = 0
        self.last_done = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            if self.status is not None:
                return httpx.Response(self.status, text="down")
            return self.fake.handler(request)
        finally:
            self.inflight -= 1
            self.last_done = time.monotonic()


async def _panel(maker, probe: Probe, services: int, **options) -> int:  # type: ignore[no-untyped-def]
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(days=10)
    async with maker() as s:
        shop = await factories.shop(s, "Sync " + secrets.token_hex(3))
        panel = await factories.panel(s, shop, PanelKind.MARZBAN, **options)
        cust = await factories.customer(s, shop)
        for i in range(services):
            name = f"cc_{secrets.token_hex(4)}_{i}"
            probe.fake.users[name] = {
                "id": i + 1,
                "username": name,
                "status": "active",
                "used_traffic": 1 * GB,
                "data_limit": 50 * GB,
                "expire": int(expire.timestamp()),
                "sub": secrets.token_hex(6),
                "request": {},
            }
            s.add(
                Service(
                    shop_id=shop.id,
                    customer_id=cust.id,
                    panel_id=panel.id,
                    panel_username=name,
                    status=ServiceStatus.ACTIVE,
                    data_limit=50 * GB,
                    expire_at=expire,
                    sub_token=secrets.token_urlsafe(12),
                )
            )
        await s.commit()
        registry.test_transports[panel.id] = httpx.MockTransport(probe)
        return panel.id


async def _synced(maker, panel_id: int) -> int:  # type: ignore[no-untyped-def]
    async with maker() as s:
        rows = await s.scalars(select(Service).where(Service.panel_id == panel_id))
        return sum(1 for row in rows if row.synced_at is not None and row.used_bytes == 1 * GB)


async def test_reads_run_in_parallel_up_to_the_driver_limit(maker) -> None:  # type: ignore[no-untyped-def]
    probe = Probe(delay=0.15)
    panel_id = await _panel(maker, probe, services=9)
    started = time.monotonic()
    results = await sync.sync_all([panel_id])
    elapsed = time.monotonic() - started
    assert results[panel_id] == (9, 0)
    assert await _synced(maker, panel_id) == 9
    assert probe.peak == 3  # Marzban's token driver: three reads in flight, never more
    serial = probe.calls * probe.delay
    assert elapsed < serial * 0.6, (elapsed, serial)


async def test_a_slow_panel_does_not_hold_up_the_others(maker) -> None:  # type: ignore[no-untyped-def]
    slow, quick = Probe(delay=0.5), Probe(delay=0.01)
    slow_id = await _panel(maker, slow, services=4, sync_concurrency=1)
    quick_id = await _panel(maker, quick, services=3)
    started = time.monotonic()
    results = await sync.sync_all([slow_id, quick_id])
    assert results == {slow_id: (4, 0), quick_id: (3, 0)}
    assert quick.last_done - started < 0.5  # finished while the slow panel was still on its first reads
    assert slow.last_done - started >= 2.0  # sync_concurrency=1: the slow panel stayed one at a time
    assert slow.peak == 1


async def test_a_down_panel_is_left_after_a_few_failures(maker) -> None:  # type: ignore[no-untyped-def]
    probe = Probe(delay=0.0, status=503)
    panel_id = await _panel(maker, probe, services=30)
    results = await sync.sync_all([panel_id])
    synced, failed = results[panel_id]  # type: ignore[misc]
    assert synced == 0 and sync.DOWN_AFTER <= failed <= sync.DOWN_AFTER + 3
    assert probe.calls <= (sync.DOWN_AFTER + 3) * 2  # a login try and a read at most per service


async def test_one_panel_error_stays_with_that_panel(maker, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    good = Probe(delay=0.0)
    good_id = await _panel(maker, good, services=2)
    bad_id = await _panel(maker, Probe(delay=0.0), services=2)
    original = sync.sync_panel_isolated

    async def explode(panel_id: int, **kwargs):  # type: ignore[no-untyped-def]
        if panel_id == bad_id:
            raise RuntimeError("synthetic")
        return await original(panel_id, **kwargs)

    monkeypatch.setattr(sync, "sync_panel_isolated", explode)
    results = await sync.sync_all([bad_id, good_id])
    assert isinstance(results[bad_id], RuntimeError) and results[good_id] == (2, 0)


async def test_read_width_follows_driver_and_panel_option(maker) -> None:  # type: ignore[no-untyped-def]
    async with maker() as s:
        shop = await factories.shop(s, "Width " + secrets.token_hex(3))
        token = await factories.panel(s, shop, PanelKind.MARZBAN)
        lowered = await factories.panel(s, shop, PanelKind.MARZBAN, sync_concurrency=1)
        raised = await factories.panel(s, shop, PanelKind.MARZBAN, sync_concurrency=50)
        cookie = await factories.panel(
            s, shop, PanelKind.XUI, inbound_id=3, sub_base_url="https://sub.example.com/sub/"
        )
        await s.commit()
        assert await sync.read_width(token) == 3
        assert await sync.read_width(lowered) == 1
        assert await sync.read_width(raised) == 3  # never above what the driver can share safely
        assert await sync.read_width(cookie) == 1  # cookie login without single-flight
        assert isinstance(await s.get(Panel, token.id), Panel)


def test_admins_can_lower_sync_concurrency_within_bounds() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.panels import _clean_options

    assert _clean_options({"sync_concurrency": 1}) == {"sync_concurrency": 1}
    for bad in (0, 9, "2", 1.5, True):
        with pytest.raises(HTTPException):
            _clean_options({"sync_concurrency": bad})
