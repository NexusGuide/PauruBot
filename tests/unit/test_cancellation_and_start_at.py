"""Unit tests for cancellation open-order guard, approval requests, and custom renewal start_at."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.core import cancellation, orders, settings
from paurubot.db.models import OrderKind, ServiceStatus


def test_normalize_renew_start_at_validation() -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    ok = orders.normalize_renew_start_at(now + timedelta(hours=2), now=now)
    assert ok == now + timedelta(hours=2)

    with pytest.raises(orders.OrderError) as naive:
        orders.normalize_renew_start_at(datetime(2026, 10, 19, 12, 0), now=now)
    assert naive.value.key == "renew_start_at_invalid"

    with pytest.raises(orders.OrderError) as too_soon:
        orders.normalize_renew_start_at(now + timedelta(minutes=3), now=now)
    assert too_soon.value.key == "renew_start_at_invalid"

    with pytest.raises(orders.OrderError) as too_far:
        orders.normalize_renew_start_at(now + timedelta(days=400), now=now)
    assert too_far.value.key == "renew_start_at_invalid"


@pytest.mark.asyncio
async def test_queued_until_picks_earliest_of_expire_at_and_custom_start(monkeypatch) -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(orders, "utcnow", lambda: now)
    monkeypatch.setattr(orders.shop_settings, "get", AsyncMock(return_value=True))

    expire = now + timedelta(days=10)
    earlier = now + timedelta(days=3)
    later = now + timedelta(days=20)
    service = SimpleNamespace(
        id=5, status=ServiceStatus.ACTIVE, expire_at=expire, data_limit=100, used_bytes=10
    )
    session = SimpleNamespace(get=AsyncMock(return_value=service))

    order_earlier = SimpleNamespace(
        shop_id=1,
        kind=OrderKind.RENEW,
        service_id=5,
        params={"queued_start_at": earlier.isoformat()},
    )
    assert await orders.queued_until(session, order_earlier) == earlier

    order_later = SimpleNamespace(
        shop_id=1,
        kind=OrderKind.RENEW,
        service_id=5,
        params={"queued_start_at": later.isoformat()},
    )
    assert await orders.queued_until(session, order_later) == expire

    # Volume-only active service (expire_at is None) still honors custom start date.
    vol_service = SimpleNamespace(
        id=6, status=ServiceStatus.ACTIVE, expire_at=None, data_limit=100, used_bytes=10
    )
    vol_session = SimpleNamespace(get=AsyncMock(return_value=vol_service))
    order_vol = SimpleNamespace(
        shop_id=1,
        kind=OrderKind.RENEW,
        service_id=6,
        params={"queued_start_at": earlier.isoformat()},
    )
    assert await orders.queued_until(vol_session, order_vol) == earlier


@pytest.mark.asyncio
async def test_cancel_blocks_service_with_open_order(monkeypatch) -> None:
    from paurubot.provisioning import lifecycle

    @asynccontextmanager
    async def fake_exclusive(_session, _service):
        yield

    monkeypatch.setattr(lifecycle, "exclusive", fake_exclusive)
    monkeypatch.setattr(
        cancellation.shop_settings, "load", AsyncMock(return_value={"self_delete_enabled": True})
    )
    monkeypatch.setattr(cancellation, "has_open_order", AsyncMock(return_value=True))

    customer = SimpleNamespace(id=10, shop_id=1)
    service = SimpleNamespace(id=20, customer_id=10, shop_id=1, status=ServiceStatus.ACTIVE)

    with pytest.raises(cancellation.CancelError) as exc:
        await cancellation.cancel(object(), customer, service)
    assert exc.value.key == "service_busy"


def test_settings_validation_for_new_section2_specs() -> None:
    settings._check("warn_time_percent", 0)
    settings._check("warn_time_percent", 25)
    with pytest.raises(settings.SettingError):
        settings._check("warn_time_percent", 100)
    with pytest.raises(settings.SettingError):
        settings._check("warn_time_percent", -1)

    settings._check("qr_background_url", "")
    settings._check("qr_background_url", "https://cdn.example.com/qr.png")
    with pytest.raises(settings.SettingError):
        settings._check("qr_background_url", "http://cdn.example.com/qr.png")
    with pytest.raises(settings.SettingError):
        settings._check("qr_caption", "x" * 801)


@pytest.mark.asyncio
async def test_cancellation_refund_override_needs_wallet_adjust() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.cancellations import Decision, decide_cancellation

    ctx = SimpleNamespace(scoped=False, can=lambda permission: False)
    with pytest.raises(HTTPException) as exc:
        await decide_cancellation(1, Decision(approve=True, refund_amount=5_000), ctx, AsyncMock())
    assert exc.value.status_code == 403
