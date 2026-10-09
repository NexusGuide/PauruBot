"""Competitor-parity ops knobs, all behind options with defaults unchanged.

Covers: panel capacity limits, subvip unique usernames, the invoice re-check guard,
monitor failure reasons, the discount expiry sweep, the admin IP allowlist,
upstream proxy settings and doctor periodic info.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from paurubot.db.models import DiscountCode, OrderKind, Panel, PanelKind, Service, ServiceStatus
from paurubot.ops import doctor
from paurubot.panels import registry
from paurubot.panels.http import SafeHttpClient, proxy_for
from paurubot.provisioning import lifecycle, monitor, sync
from paurubot.security import login_guard
from tests import factories


def _panel_row(**options) -> Panel:  # type: ignore[no-untyped-def]
    return Panel(
        shop_id=1,
        name="p",
        kind=PanelKind.MARZBAN,
        base_url="https://panel.example.com",
        username="admin",
        password_enc="enc",
        options=options,
    )


def test_capacity_helpers_default_to_unlimited() -> None:
    assert lifecycle.capacity_limit_of(None) is None
    assert lifecycle.capacity_limit_of({}) is None
    assert lifecycle.capacity_limit_of({"capacity_limit": 0}) is None
    assert lifecycle.capacity_limit_of({"capacity_limit": "many"}) is None
    assert lifecycle.capacity_limit_of({"capacity_limit": 3}) == 3
    assert not lifecycle.capacity_reached(99, {})
    assert not lifecycle.capacity_reached(2, {"capacity_limit": 3})
    assert lifecycle.capacity_reached(3, {"capacity_limit": 3})


async def test_panel_capacity_gating(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    open_panel = await factories.panel(session, shop)
    assert await lifecycle.panel_capacity_reached(session, open_panel) is False

    capped = await factories.panel(session, shop, PanelKind.MARZBAN, capacity_limit=1)
    assert await lifecycle.panel_capacity_reached(session, capped) is False
    cust = await factories.customer(session, shop)
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=capped.id,
            panel_username="pb_1",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.flush()
    assert await lifecycle.panel_capacity_reached(session, capped) is True


async def test_capacity_blocks_create_path(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import orders
    from paurubot.ledger import wallet
    from tests.fakes.panels import FakeTokenPanel, transport

    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN, capacity_limit=1)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    try:
        plan = await factories.plan(session, shop, panel)
        cust = await factories.customer(session, shop)
        session.add(
            Service(
                shop_id=shop.id,
                customer_id=cust.id,
                panel_id=panel.id,
                panel_username="pb_full",
                status=ServiceStatus.ACTIVE,
            )
        )
        await session.flush()
        await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
        order = await orders.create(
            session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
        )
        await orders.settle_from_wallet(session, order.id)
        await session.commit()
        with pytest.raises(lifecycle.ProvisionError, match="panel_full"):
            await lifecycle._create_service(session, order)
    finally:
        registry.test_transports.clear()
        registry._cache.clear()


async def test_ensure_unique_username(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    cust = await factories.customer(session, shop)
    assert await lifecycle.ensure_unique_username(session, panel.id, "pb_free") == "pb_free"
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=panel.id,
            panel_username="pb_taken",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.flush()
    assert await lifecycle.ensure_unique_username(session, panel.id, "pb_taken") == "pb_taken_2"


async def test_subvip_unique_renames_on_collision(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import orders
    from paurubot.ledger import wallet
    from tests.fakes.panels import FakeTokenPanel, transport

    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN, subvip_unique=True)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    try:
        plan = await factories.plan(session, shop, panel)
        cust = await factories.customer(session, shop)
        session.add(
            Service(
                shop_id=shop.id,
                customer_id=cust.id,
                panel_id=panel.id,
                panel_username="pb_vip",
                status=ServiceStatus.ACTIVE,
            )
        )
        await session.flush()
        await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
        order = await orders.create(
            session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
        )
        await orders.settle_from_wallet(session, order.id)
        order.params = {**order.params, "panel_id": panel.id, "panel_username": "pb_vip"}
        await session.commit()
        service = await lifecycle._create_service(session, order)
        assert service.panel_username == "pb_vip_2"
    finally:
        registry.test_transports.clear()
        registry._cache.clear()


def test_emergency_and_national_helpers() -> None:
    plain = _panel_row()
    assert not registry.is_emergency(plain) and not registry.is_national(plain)
    emergency = _panel_row(is_emergency=True, national_net_only=True)
    assert registry.is_emergency(emergency) and registry.is_national(emergency)
    assert registry.emergency_panels([plain, emergency]) == [emergency]


def test_invoice_recheck_due() -> None:
    now = datetime.now(UTC)
    assert sync.invoice_recheck_due(None, now) is True
    assert sync.invoice_recheck_due((now - timedelta(seconds=100)).isoformat(), now) is False
    assert sync.invoice_recheck_due(now - timedelta(seconds=1600), now) is True
    assert sync.invoice_recheck_due(now - timedelta(hours=1), now) is True
    assert sync.invoice_recheck_due("not-a-date", now) is True


async def test_evaluate_alerts_guard_window(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        panel_username="pb_1",
        status=ServiceStatus.EXPIRED,
        expire_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(service)
    await session.flush()
    assert await sync.evaluate_alerts(session, service, ServiceStatus.ACTIVE) == ["expired"]
    service.expire_at = datetime.now(UTC) - timedelta(days=2)  # re-armed inside the guard window
    assert await sync.evaluate_alerts(session, service, ServiceStatus.ACTIVE) == []


def test_classify_panel_error() -> None:
    assert monitor.classify_panel_error("API key rejected") == "token_invalid"
    assert monitor.classify_panel_error("login failed (401)") == "token_invalid"
    assert monitor.classify_panel_error("xray stopped unexpectedly") == "xray_stopped"
    assert monitor.classify_panel_error("connection failed: ConnectError") == "unreachable"
    assert monitor.classify_panel_error(None) == "unreachable"


async def test_expire_discount_codes(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.worker import jobs as worker_jobs

    shop = await factories.shop(session)
    now = datetime.now(UTC)
    past = DiscountCode(shop_id=shop.id, code="OLD", expires_at=now - timedelta(hours=1))
    future = DiscountCode(shop_id=shop.id, code="NEW", expires_at=now + timedelta(days=1))
    forever = DiscountCode(shop_id=shop.id, code="FOREVER")
    session.add_all([past, future, forever])
    await session.flush()
    assert await worker_jobs.expire_discount_codes_in(session) == 1
    await session.refresh(past)
    await session.refresh(future)
    await session.refresh(forever)
    assert past.is_active is False and future.is_active and forever.is_active


def test_is_admin_ip_allowed() -> None:
    assert login_guard.is_admin_ip_allowed("10.0.0.9", None) is True
    assert login_guard.is_admin_ip_allowed("10.0.0.9", []) is True
    assert login_guard.is_admin_ip_allowed("10.0.0.9", ["10.0.0.9"]) is True
    assert login_guard.is_admin_ip_allowed("10.0.0.9", ["10.0.0.0/8"]) is True
    assert login_guard.is_admin_ip_allowed("10.0.0.9", ["192.168.0.0/16", "10.0.0.0/24"]) is True
    assert login_guard.is_admin_ip_allowed("203.0.113.9", ["10.0.0.0/8"]) is False
    assert login_guard.is_admin_ip_allowed("not-an-ip", ["10.0.0.0/8"]) is False
    assert login_guard.is_admin_ip_allowed("2001:db8::1", ["2001:db8::/32"]) is True
    assert login_guard.is_admin_ip_allowed("10.0.0.9", ["office.example.com"]) is False


async def test_login_check_denies_outside_allowlist() -> None:
    with pytest.raises(login_guard.LoginLocked):
        await login_guard.check("admin", "203.0.113.9", allowlist=["10.0.0.0/8"])


def test_proxy_for(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert proxy_for("https://panel.example.com") is None
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    assert proxy_for("https://panel.example.com") == "http://proxy:8080"
    assert proxy_for("http://panel.example.com") is None
    monkeypatch.setenv("NO_PROXY", "panel.example.com")
    assert proxy_for("https://panel.example.com") is None


async def test_client_proxy_attribute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    plain = SafeHttpClient(
        "https://panel.example.com", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert plain.proxy is None
    await plain.aclose()
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    proxied = SafeHttpClient(
        "https://panel.example.com", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert proxied.proxy == "http://proxy:8080"
    await proxied.aclose()


def test_doctor_periodic_info_lists_jobs() -> None:
    detail = doctor.periodic_info()
    assert "discounts.expire" in detail and "expiry.sweep" in detail
