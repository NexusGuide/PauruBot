"""The existing /me response exposes only the current context's warning settings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.api.routes import webapp
from paurubot.core import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("tonpay_available", [False, True])
async def test_me_projects_each_context_policy_without_secrets_or_preference_writes(
    monkeypatch, tonpay_available
):
    monkeypatch.setattr(webapp, "wallet_balance", AsyncMock(return_value=100))
    monkeypatch.setattr(webapp.trials, "eligibility", AsyncMock(return_value="disabled"))
    monkeypatch.setattr(webapp.customers, "referral_count", AsyncMock(return_value=0))
    monkeypatch.setattr(webapp.panel_accounts, "available", AsyncMock(return_value=False))
    monkeypatch.setattr(webapp.payments, "available", AsyncMock(return_value=["card"]))
    monkeypatch.setattr(webapp.payments, "topup_bounds", AsyncMock(return_value=(1, 1000)))
    custom_available = AsyncMock(return_value=tonpay_available)
    monkeypatch.setattr(webapp, "_tonpay_available", custom_available)
    monkeypatch.setattr(webapp.webapp_staff, "describe", AsyncMock(return_value=None))
    monkeypatch.setattr(
        webapp.marketing, "view", lambda customer: {"marketing_opt_out": True, "changed_at": None}
    )
    monkeypatch.setattr(
        webapp.customer_preferences,
        "profile_view",
        lambda customer: {"phone_masked": None, "member_since": "2026-01-01"},
    )
    customer = SimpleNamespace(
        id=7,
        telegram_id=1007,
        first_name="Synthetic",
        username=None,
        locale="fa",
        reseller_profile=None,
        alerts_muted=True,
    )
    for shop_id, percent, days in [(1, 10, 2), (2, 30, 4), (3, 0, 0)]:
        cfg = settings.defaults()
        cfg.update(warn_remaining_percent=percent, warn_days=days, zarinpal_merchant="SYNTHETIC-SECRET")
        shop = SimpleNamespace(
            id=shop_id, title="Synthetic", bot_username=None, default_locale="fa", currency="IRT"
        )
        ctx = SimpleNamespace(customer=customer, shop=shop, cfg=cfg, session=object())
        result = await webapp.me(ctx)
        assert result["alert_policy"] == {"warn_remaining_percent": percent, "warn_days": days}
        assert result["alerts_muted"] is True
        assert result["preferences"]["marketing_opt_out"] is True
        assert cfg["zarinpal_merchant"] == "SYNTHETIC-SECRET"
        assert set(result["alert_policy"]) == {"warn_remaining_percent", "warn_days"}
        assert result["tonpay_custom_available"] is tonpay_available
        assert "SYNTHETIC-SECRET" not in str(result)
        custom_available.assert_awaited_with(ctx)
    assert custom_available.await_count == 3
