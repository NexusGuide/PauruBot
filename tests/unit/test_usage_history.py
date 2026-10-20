"""F-06: actual counters, bounded storage and no fabricated consumption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from paurubot.core import usage_history
from paurubot.db.models import Service, ServiceStatus, ServiceUsage
from paurubot.panels.base import PanelUser, usage_counter
from paurubot.provisioning import lifecycle, sync
from tests import factories

NOW = datetime(2026, 10, 14, 10, 5, tzinfo=UTC)


async def _service(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="history-user",
        status=ServiceStatus.ACTIVE,
        data_limit=10000,
    )
    session.add(service)
    await session.flush()
    return service, panel


def _info(value: int, *, known: bool = True) -> PanelUser:
    return PanelUser("history-user", used_bytes=value, usage_known=known, data_limit=10000)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (42, 42),
        ("0", 0),
        ("123", 123),
        (None, None),
        (True, None),
        (False, None),
        (-1, None),
        ("-1", None),
        ("", None),
        ("1.5", None),
        (1.5, None),
        (float("nan"), None),
        (2**53, None),
        ("۹", None),
    ],
)
def test_only_exact_reported_safe_counters(raw, expected) -> None:  # type: ignore[no-untyped-def]
    assert usage_counter(raw) == expected


async def test_unknown_usage_does_not_become_a_zero_point(session) -> None:  # type: ignore[no-untyped-def]
    service, _ = await _service(session)
    assert not await usage_history.observe(session, service, _info(0, known=False), source="poll", at=NOW)
    assert (await usage_history.series(session, service, 7, now=NOW))["points"] == []
    assert await usage_history.observe(session, service, _info(0), source="poll", at=NOW)
    assert (await usage_history.series(session, service, 7, now=NOW))["points"][0]["used_bytes"] == 0


async def test_hourly_last_observation_and_replay_guard(session) -> None:  # type: ignore[no-untyped-def]
    service, _ = await _service(session)
    await usage_history.observe(session, service, _info(100), source="poll", at=NOW)
    await usage_history.observe(
        session, service, _info(150), source="webhook", at=NOW + timedelta(minutes=10)
    )
    assert not await usage_history.observe(session, service, _info(120), source="poll", at=NOW)
    rows = list(await session.scalars(select(ServiceUsage).where(ServiceUsage.service_id == service.id)))
    assert len(rows) == 1 and rows[0].used_bytes == 150 and rows[0].source == "webhook"
    result = await usage_history.series(session, service, 7, now=NOW + timedelta(hours=1))
    assert (
        len(result["points"]) == 1 and result["points"][0]["at"] == (NOW + timedelta(minutes=10)).isoformat()
    )


async def test_drop_within_hour_stays_a_break_and_period_change_breaks(session) -> None:  # type: ignore[no-untyped-def]
    service, _ = await _service(session)
    await usage_history.observe(session, service, _info(100), source="poll", at=NOW)
    await usage_history.observe(session, service, _info(20), source="poll", at=NOW + timedelta(minutes=5))
    await usage_history.observe(session, service, _info(110), source="poll", at=NOW + timedelta(minutes=10))
    service.data_limit = 20000
    await usage_history.observe(session, service, _info(150), source="poll", at=NOW + timedelta(hours=1))
    result = await usage_history.series(session, service, 7, now=NOW + timedelta(hours=2))
    assert len(result["points"]) == 2
    assert all(p["break_before"] for p in result["points"])


async def test_gap_is_not_filled_or_joined_and_window_is_bounded(session) -> None:  # type: ignore[no-untyped-def]
    service, _ = await _service(session)
    await usage_history.observe(session, service, _info(100), source="poll", at=NOW - timedelta(days=8))
    await usage_history.observe(session, service, _info(200), source="poll", at=NOW - timedelta(hours=4))
    await usage_history.observe(session, service, _info(300), source="poll", at=NOW)
    result = await usage_history.series(session, service, 7, now=NOW)
    assert [p["used_bytes"] for p in result["points"]] == [200, 300]
    assert result["points"][1]["break_before"] is True
    assert result["last_observed_at"] == NOW.isoformat()
    with pytest.raises(ValueError):
        await usage_history.series(session, service, 31, now=NOW)
    with pytest.raises(ValueError):
        await usage_history.observe(session, service, _info(400), source="poll", at=NOW.replace(tzinfo=None))


async def test_retention_is_batched_and_service_delete_cascades(session) -> None:  # type: ignore[no-untyped-def]
    service, _ = await _service(session)
    for days in (40, 39, 38, 1):
        await usage_history.observe(
            session, service, _info(100), source="poll", at=NOW - timedelta(days=days)
        )
    assert await usage_history.prune(session, now=NOW, batch=2) == 2
    assert await usage_history.prune(session, now=NOW, batch=2) == 1
    result = await usage_history.series(session, service, 30, now=NOW)
    assert len(result["points"]) == 1
    await session.delete(service)
    await session.flush()
    assert (
        list(await session.scalars(select(ServiceUsage).where(ServiceUsage.service_id == service.id))) == []
    )


async def test_successful_refresh_and_webhook_record_but_missing_user_does_not(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service, panel = await _service(session)
    driver = SimpleNamespace(get_user=AsyncMock(return_value=_info(100)))
    monkeypatch.setattr(lifecycle, "driver_for", AsyncMock(return_value=driver))
    monkeypatch.setattr(sync, "evaluate_alerts", AsyncMock())
    await lifecycle.refresh(session, service)
    assert len((await usage_history.series(session, service, 7))["points"]) == 1
    await sync.apply_webhook_user(session, panel, _info(200))
    assert (await usage_history.series(session, service, 7))["points"][-1]["used_bytes"] == 100
    # The authoritative read, not the event, provides the next real sample.
    driver.get_user.return_value = _info(200)
    await lifecycle.refresh(session, service)
    assert (await usage_history.series(session, service, 7))["points"][-1]["used_bytes"] == 200
    driver.get_user.return_value = None
    await lifecycle.refresh(session, service)
    assert service.status == ServiceStatus.DELETED
    assert (await usage_history.series(session, service, 7))["points"][-1]["used_bytes"] == 200
