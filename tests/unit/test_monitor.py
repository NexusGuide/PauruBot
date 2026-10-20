from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from paurubot.core import catalog
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, PanelCheck, PanelKind
from paurubot.panels import registry
from paurubot.provisioning import monitor
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


@pytest.fixture(autouse=True)
async def isolate(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield
    registry.test_transports.clear()
    registry._cache.clear()


async def _panel(session, kind=PanelKind.MARZBAN):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, kind)
    fake = FakeTokenPanel(kind.value)
    registry.test_transports[panel.id] = transport(fake)
    await session.commit()
    return shop, panel, fake


async def _alerts(session) -> list[dict]:  # type: ignore[no-untyped-def]
    # PostgreSQL does not promise insertion order without ORDER BY.
    jobs = await session.scalars(
        select(Job).where(Job.kind == "panel_alert", Job.status == JobStatus.QUEUED).order_by(Job.id)
    )
    return [j.payload for j in jobs]


async def test_down_alert_after_two_failures_and_recovery(session) -> None:  # type: ignore[no-untyped-def]
    _, panel, fake = await _panel(session)
    t0 = utcnow()
    first = await monitor.check_panel(session, panel, now=t0)
    assert first.ok and panel.is_healthy and panel.stats["cpu_percent"] == 12.5
    assert panel.stats["mem_total"] == 8 * 1024**3

    fake.fail_next = 500
    await monitor.check_panel(session, panel, now=t0 + timedelta(minutes=2))
    assert panel.down_since is None and await _alerts(session) == []  # one blip is not an outage
    fake.fail_next = 500
    await monitor.check_panel(session, panel, now=t0 + timedelta(minutes=4))
    assert panel.down_since is not None and panel.is_healthy is False
    assert [a["event"] for a in await _alerts(session)] == ["down"]

    back = await monitor.check_panel(session, panel, now=t0 + timedelta(minutes=14))
    assert back.events == ["up"] and panel.down_since is None
    assert (await _alerts(session))[-1]["minutes"] == 10
    up = await monitor.uptime(session, [panel.id])
    assert up[panel.id]["day"] == 50.0
    checks = list(
        await session.scalars(
            select(PanelCheck)
            .where(PanelCheck.panel_id == panel.id)
            .order_by(PanelCheck.checked_at, PanelCheck.id)
        )
    )
    assert [c.ok for c in checks] == [True, False, False, True]


async def test_node_changes_raise_alerts(session) -> None:  # type: ignore[no-untyped-def]
    _, panel, fake = await _panel(session)
    fake.nodes = [
        {"id": 1, "name": "de-1", "status": "connected"},
        {"id": 2, "name": "nl-1", "status": "connected"},
    ]
    await monitor.check_panel(session, panel)
    fake.nodes[1]["status"] = "error"
    result = await monitor.check_panel(session, panel)
    assert result.events == ["node_down:nl-1"]
    fake.nodes[1]["status"] = "connected"
    assert (await monitor.check_panel(session, panel)).events == ["node_up:nl-1"]


async def test_pasarguard_quota_blocks_sales_and_warns(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, fake = await _panel(session, PanelKind.PASARGUARD)
    fake.admin = {
        "username": "bot",
        "role": {"name": "reseller", "limits": {"max_users": 10}},
        "total_users": 9,
    }
    result = await monitor.check_panel(session, panel)
    assert panel.stats["admin"]["max_users"] == 10 and panel.stats["admin"]["owner"] is False
    assert "quota" in result.events
    assert "quota" not in (await monitor.check_panel(session, panel)).events  # once a day
    assert await catalog.pick_panel_for(session, shop.id, panel.id, None) is not None
    fake.admin["total_users"] = 10
    await monitor.check_panel(session, panel)
    assert monitor.quota_full(panel)
    assert await catalog.pick_panel_for(session, shop.id, panel.id, None) is None


async def test_health_updates_keep_the_cached_driver(session) -> None:  # type: ignore[no-untyped-def]
    _, panel, fake = await _panel(session)
    await monitor.check_panel(session, panel)
    await session.commit()
    await monitor.check_panel(session, panel)
    await session.commit()
    assert fake.logins == 1
