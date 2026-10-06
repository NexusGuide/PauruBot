"""Runtime settings stored in the database and editable from the admin panels."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Setting

DEFAULTS: dict[str, Any] = {
    "bot_enabled": True,
    "channels": [],
    "card_enabled": True,
    "card_numbers": [],
    "zarinpal_enabled": False,
    "zarinpal_merchant": "",
    "zarinpal_sandbox": False,
    "stars_enabled": False,
    "stars_rate": 2000,
    "min_topup": 20000,
    "max_topup": 20000000,
    "payment_expire_hours": 24,
    "test_enabled": True,
    "test_panel_id": None,
    "test_days": 1,
    "test_gb": 1,
    "referral_enabled": True,
    "referral_percent": 10,
    "reseller_requests_enabled": True,
    "support_username": "",
    "warn_remaining_percent": 10,
    "warn_days": 2,
    "delete_expired_after_days": 7,
    "renew_resets_usage": True,
    "report_chat_id": None,
    "require_phone": False,
    "texts": {},
}

# Settings shown and editable in admin UIs: key -> value type
EDITABLE: dict[str, type] = {
    "bot_enabled": bool,
    "card_enabled": bool,
    "zarinpal_enabled": bool,
    "zarinpal_merchant": str,
    "zarinpal_sandbox": bool,
    "stars_enabled": bool,
    "stars_rate": int,
    "min_topup": int,
    "max_topup": int,
    "payment_expire_hours": int,
    "test_enabled": bool,
    "test_panel_id": int,
    "test_days": int,
    "test_gb": int,
    "referral_enabled": bool,
    "referral_percent": int,
    "reseller_requests_enabled": bool,
    "support_username": str,
    "warn_remaining_percent": int,
    "warn_days": int,
    "delete_expired_after_days": int,
    "renew_resets_usage": bool,
    "report_chat_id": int,
    "require_phone": bool,
}

_cache: dict[str, Any] | None = None


async def load(session: AsyncSession) -> dict[str, Any]:
    global _cache
    rows = (await session.scalars(select(Setting))).all()
    data = copy.deepcopy(DEFAULTS)
    for row in rows:
        data[row.key] = row.value
    _cache = data
    return data


def cached() -> dict[str, Any]:
    return _cache if _cache is not None else copy.deepcopy(DEFAULTS)


def get(key: str) -> Any:
    return cached().get(key, DEFAULTS.get(key))


async def set_value(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.flush()
    cached_data = cached()
    cached_data[key] = value
    global _cache
    _cache = cached_data


def parse_value(key: str, raw: str) -> Any:
    kind = EDITABLE.get(key)
    if kind is None:
        raise KeyError(key)
    raw = raw.strip()
    if kind is bool:
        lowered = raw.lower()
        if lowered in {"1", "true", "on", "yes"}:
            return True
        if lowered in {"0", "false", "off", "no"}:
            return False
        raise ValueError("boolean expected")
    if kind is int:
        if raw.lower() in {"", "none", "null", "-"}:
            return None
        return int(raw)
    return raw
