"""Settings validation and existing encrypted-storage/masking wiring (no real credentials)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from paurubot.core import settings

pytestmark = pytest.mark.security


def test_custom_method_defaults_fail_closed_and_mask_keys():
    cfg = settings.defaults()
    assert not cfg["tonpay_custom_enabled"] and not cfg["tonpay_custom_confirm_enabled"]
    assert not cfg["tonpay_custom_policy_approved"]
    assert all(cfg["tonpay_custom_" + k] == 0 for k in ["receipt_wait", "card_cooldown", "review_window"])
    cfg["tonpay_custom_api_key"] = "synthetic-secret"
    assert settings.public_view(cfg)["tonpay_custom_api_key"] == {"set": True}
    assert "synthetic-secret" not in str(settings.public_view(cfg))


async def test_new_key_uses_existing_encryptor_and_rotates_epoch(monkeypatch):  # type: ignore[no-untyped-def]
    cfg = settings.defaults()
    cfg["tonpay_custom_credential_epoch"] = "old-epoch"
    monkeypatch.setattr(settings, "load", AsyncMock(return_value=cfg))
    encrypt = Mock(return_value="encrypted-synthetic")
    monkeypatch.setattr(settings.secretbox, "encrypt", encrypt)
    db = SimpleNamespace(execute=AsyncMock())
    result = await settings.set_many(db, 7, {"tonpay_custom_api_key": "synthetic-secret"})
    encrypt.assert_called_once_with("payment", "synthetic-secret")
    assert result["tonpay_custom_credential_epoch"] != "old-epoch"
    params = [c.args[0].compile(dialect=postgresql.dialect()).params for c in db.execute.await_args_list]
    stored = next(p["value"] for p in params if p["key"] == "tonpay_custom_api_key")
    assert stored == {"enc": "encrypted-synthetic"}
    assert "synthetic-secret" not in str(params)


@pytest.mark.parametrize("value", [True, 1.5, "1.5", "-1"])
async def test_policy_cannot_be_coerced_from_ambiguous_value(monkeypatch, value):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(settings, "load", AsyncMock(return_value=settings.defaults()))
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(settings.SettingError):
        await settings.set_many(db, 7, {"tonpay_custom_receipt_wait": value})
    db.execute.assert_not_awaited()


async def test_enable_requires_separate_confirmation_gate(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(settings, "load", AsyncMock(return_value=settings.defaults()))
    db = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(settings.SettingError):
        await settings.set_many(db, 7, {"tonpay_custom_enabled": True})
    db.execute.assert_not_awaited()
