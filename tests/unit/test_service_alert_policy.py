"""Threshold projection and worker concordance. No external providers are contacted."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.core import alert_policy, settings
from paurubot.db.models import ServiceStatus
from paurubot.provisioning import sync

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "alert_policy_cases.json").read_text())
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def test_projection_is_only_two_non_secret_thresholds_without_mutating_cfg():
    cfg = settings.defaults()
    cfg.update(zarinpal_merchant="SYNTHETIC-SECRET", warn_days=4, warn_remaining_percent=30)
    original = dict(cfg)
    assert alert_policy.service_alert_policy(cfg) == {"warn_days": 4, "warn_remaining_percent": 30}
    assert cfg == original
    assert "SYNTHETIC-SECRET" not in json.dumps(alert_policy.service_alert_policy(cfg))


@pytest.mark.parametrize(
    "percent,days,expected",
    [
        (10, 2, {"warn_remaining_percent": 10, "warn_days": 2}),
        (None, None, {"warn_remaining_percent": 0, "warn_days": 0}),
        (0, 0, {"warn_remaining_percent": 0, "warn_days": 0}),
        (-1, -2, {"warn_remaining_percent": -1, "warn_days": -2}),
        (120, 40, {"warn_remaining_percent": 120, "warn_days": 40}),
        ("10", "2", {"warn_remaining_percent": 10, "warn_days": 2}),
    ],
)
def test_projection_preserves_existing_integer_semantics(percent, days, expected):
    assert (
        alert_policy.service_alert_policy({"warn_remaining_percent": percent, "warn_days": days}) == expected
    )


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_worker_uses_the_same_threshold_cases_as_the_miniapp(monkeypatch, case):
    monkeypatch.setattr(sync, "datetime", FixedDatetime)
    monkeypatch.setattr(sync.shop_settings, "load", AsyncMock(return_value=case["policy"]))
    monkeypatch.setattr(sync.orders, "has_queued", AsyncMock(return_value=False))
    monkeypatch.setattr(sync.orders, "release_queued", AsyncMock(return_value=False))
    enqueue = AsyncMock()
    monkeypatch.setattr(sync.queue, "enqueue", enqueue)
    service = SimpleNamespace(
        id=7,
        shop_id=42,
        status=ServiceStatus(case["status"]),
        data_limit=case["data_limit"],
        used_bytes=case["data_limit"] - case["remaining"],
        alerts_sent={},
        expire_at=NOW + timedelta(seconds=case["seconds"]) if case["seconds"] is not None else None,
    )
    result = await sync.evaluate_alerts(object(), service, ServiceStatus.ACTIVE)
    assert ("low_volume" in result) == case["low_volume"]
    assert ("expiring" in result) == case["expiring"]
    for call in enqueue.await_args_list:
        assert call.kwargs["shop_id"] == 42
        assert call.args[3]["service_id"] == 7
    if "expiring" in result:
        payload = next(c.args[3] for c in enqueue.await_args_list if c.args[3]["alert"] == "expiring")
        assert payload["days"] == max(1, int(case["seconds"] // 86400) + 1)


@pytest.mark.asyncio
async def test_already_queued_renewal_still_suppresses_service_alerts(monkeypatch):
    monkeypatch.setattr(
        sync.shop_settings,
        "load",
        AsyncMock(return_value={"warn_days": "broken-unused", "warn_remaining_percent": "broken-unused"}),
    )
    monkeypatch.setattr(sync.orders, "has_queued", AsyncMock(return_value=True))
    enqueue = AsyncMock()
    monkeypatch.setattr(sync.queue, "enqueue", enqueue)
    service = SimpleNamespace(
        id=7, shop_id=42, status=ServiceStatus.ACTIVE, expire_at=NOW + timedelta(seconds=1), alerts_sent={}
    )
    assert await sync.evaluate_alerts(object(), service, ServiceStatus.ACTIVE) == []
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [True, False])
async def test_lapsed_service_keeps_release_and_terminal_alert_paths_ahead_of_unused_policy(
    monkeypatch, released
):
    monkeypatch.setattr(
        sync.shop_settings,
        "load",
        AsyncMock(return_value={"warn_days": "broken-unused", "warn_remaining_percent": "broken-unused"}),
    )
    release = AsyncMock(return_value=released)
    monkeypatch.setattr(sync.orders, "release_queued", release)
    monkeypatch.setattr(sync.orders, "has_queued", AsyncMock(return_value=False))
    enqueue = AsyncMock()
    monkeypatch.setattr(sync.queue, "enqueue", enqueue)
    service = SimpleNamespace(
        id=7, shop_id=42, status=ServiceStatus.EXPIRED, expire_at=NOW - timedelta(days=1), alerts_sent={}
    )
    result = await sync.evaluate_alerts(object(), service, ServiceStatus.ACTIVE)
    release.assert_awaited_once()
    assert result == ([] if released else ["expired"])
    if released:
        enqueue.assert_not_awaited()
    else:
        enqueue.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,expires,cfg,expected",
    [
        (0, False, {"warn_remaining_percent": "unused-bad", "warn_days": "unused-bad"}, []),
        (100, False, {"warn_remaining_percent": 10, "warn_days": "unused-bad"}, []),
        (0, True, {"warn_remaining_percent": "unused-bad", "warn_days": 2}, ["expiring"]),
    ],
)
async def test_worker_keeps_original_lazy_setting_reads(monkeypatch, limit, expires, cfg, expected):
    monkeypatch.setattr(sync, "datetime", FixedDatetime)
    monkeypatch.setattr(sync.shop_settings, "load", AsyncMock(return_value=cfg))
    monkeypatch.setattr(sync.orders, "has_queued", AsyncMock(return_value=False))
    monkeypatch.setattr(sync.queue, "enqueue", AsyncMock())
    service = SimpleNamespace(
        id=7,
        shop_id=42,
        status=ServiceStatus.ACTIVE,
        data_limit=limit,
        used_bytes=0,
        expire_at=NOW + timedelta(seconds=1) if expires else None,
        alerts_sent={},
    )
    assert await sync.evaluate_alerts(object(), service, ServiceStatus.ACTIVE) == expected


@pytest.mark.parametrize(
    "hour,start,end,wait_hours",
    [
        (3, 0, 8, 5),  # inside 00–08: sent at 08:00
        (9, 0, 8, None),  # outside
        (23, 22, 7, 8),  # a window over midnight
        (6, 22, 7, 1),
        (12, 22, 7, None),
        (3, None, 8, None),  # off unless both ends are set
        (3, 5, 5, None),  # an empty window
    ],
)
def test_quiet_hours_hold_customer_alerts_until_morning(hour, start, end, wait_hours):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Tehran")
    now = datetime(2026, 10, 18, hour, 20, tzinfo=tz)
    until = alert_policy.quiet_until(now, {"alerts_quiet_start": start, "alerts_quiet_end": end}, tz)
    if wait_hours is None:
        assert until is None
    else:
        assert until is not None and until.astimezone(tz).hour == end and until.minute == 0
        assert timedelta(hours=wait_hours - 1) < until - now <= timedelta(hours=wait_hours)


@pytest.mark.asyncio
async def test_alerts_inside_quiet_hours_are_queued_for_later(monkeypatch):
    monkeypatch.setattr(sync, "datetime", FixedDatetime)
    policy = {"warn_days": 3, "warn_remaining_percent": 0, "alerts_quiet_start": 0, "alerts_quiet_end": 23}
    monkeypatch.setattr(sync.shop_settings, "load", AsyncMock(return_value=policy))
    monkeypatch.setattr(sync.orders, "has_queued", AsyncMock(return_value=False))
    monkeypatch.setattr(sync.orders, "release_queued", AsyncMock(return_value=False))
    monkeypatch.setattr(sync.alert_policy, "quiet_until", lambda now, cfg, tz: NOW + timedelta(hours=5))
    enqueue = AsyncMock()
    monkeypatch.setattr(sync.queue, "enqueue", enqueue)
    service = SimpleNamespace(
        id=7,
        shop_id=42,
        status=ServiceStatus.ACTIVE,
        data_limit=0,
        used_bytes=0,
        alerts_sent={},
        expire_at=NOW + timedelta(days=1),
    )
    assert await sync.evaluate_alerts(object(), service, ServiceStatus.ACTIVE) == ["expiring"]
    assert enqueue.await_args.kwargs["run_at"] == NOW + timedelta(hours=5)
