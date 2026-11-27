from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.core import settings
from paurubot.db.models import ShopSetting
from tests import factories


async def test_defaults_and_typed_updates(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    data = await settings.load(session, shop.id, fresh=True)
    assert data["trial_enabled"] is True
    await settings.set_many(session, shop.id, {"trial_days": "3", "trial_enabled": "off"})
    data = await settings.load(session, shop.id)
    assert data["trial_days"] == 3 and data["trial_enabled"] is False
    with pytest.raises(settings.SettingError):
        await settings.set_many(session, shop.id, {"trial_days": "many"})
    with pytest.raises(settings.SettingError):
        await settings.set_many(session, shop.id, {"no_such_key": 1})


async def test_secret_settings_are_encrypted_at_rest(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await settings.set_many(session, shop.id, {"zarinpal_merchant": "merchant-1234-secret"})
    row = await session.scalar(
        select(ShopSetting).where(ShopSetting.shop_id == shop.id, ShopSetting.key == "zarinpal_merchant")
    )
    assert "merchant-1234-secret" not in str(row.value)
    data = await settings.load(session, shop.id, fresh=True)
    assert data["zarinpal_merchant"] == "merchant-1234-secret"
    assert settings.public_view(data)["zarinpal_merchant"] == {"set": True}


def test_cron_overrides_and_support_faq_are_validated() -> None:
    # A cron override must reach its own validation; it used to fall into the FAQ block.
    settings._check("cron_overrides", {"worker.heartbeat": {"enabled": True}})
    settings._check("cron_overrides", {"trial.reset": {"interval_seconds": 300}})
    with pytest.raises(settings.SettingError):
        settings._check("cron_overrides", {"worker.heartbeat": {"enabled": False}})
    with pytest.raises(settings.SettingError):
        settings._check("cron_overrides", {"trial.reset": {"interval_seconds": 5}})
    settings._check("service_import_panel_ids", [1, 2])
    with pytest.raises(settings.SettingError):
        settings._check("service_import_panel_ids", ["x"])

    valid = [{"q": "How?", "a": "Like this", "btn_text": "Open", "btn_url": "https://example.com"}]
    settings._check("support_faq", valid)
    with pytest.raises(settings.SettingError):
        settings._check("support_faq", [{"q": "How?"}])
    with pytest.raises(settings.SettingError):
        settings._check("support_faq", [{"q": "How?", "a": "Like this", "btn_text": "Open"}])


async def test_cron_overrides_round_trip_through_set_many(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await settings.set_many(session, shop.id, {"cron_overrides": {"worker.heartbeat": {"enabled": True}}})
    data = await settings.load(session, shop.id, fresh=True)
    assert data["cron_overrides"]["worker.heartbeat"]["enabled"] is True
