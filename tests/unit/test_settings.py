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
