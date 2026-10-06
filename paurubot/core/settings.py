"""Per-shop runtime settings with typed defaults, stored in `shop_settings`."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import ShopSetting
from paurubot.security import secretbox


@dataclass(frozen=True, slots=True)
class Spec:
    kind: type
    default: Any
    group: str
    secret: bool = False


SPECS: dict[str, Spec] = {
    # general
    "bot_enabled": Spec(bool, True, "general"),
    "languages": Spec(list, ["fa", "en"], "general"),
    "require_phone": Spec(bool, False, "general"),
    "phone_country_codes": Spec(list, [], "general"),
    "support_username": Spec(str, "", "general"),
    "terms_required": Spec(bool, False, "general"),
    "terms_version": Spec(int, 1, "general"),
    # trial (CAT-04)
    "trial_enabled": Spec(bool, True, "trial"),
    "trial_panel_id": Spec(int, None, "trial"),
    "trial_days": Spec(int, 1, "trial"),
    "trial_gb": Spec(int, 1, "trial"),
    "trial_generation": Spec(int, 1, "trial"),
    "trial_daily_quota": Spec(int, 0, "trial"),
    "trial_requires_phone": Spec(bool, False, "trial"),
    # referral (GRW-01)
    "referral_enabled": Spec(bool, True, "growth"),
    "referral_percent": Spec(int, 10, "growth"),
    # payments
    "min_topup": Spec(int, 20_000, "payments"),
    "max_topup": Spec(int, 50_000_000, "payments"),
    "order_expire_minutes": Spec(int, 60, "payments"),
    "payment_expire_minutes": Spec(int, 60, "payments"),
    "card_enabled": Spec(bool, True, "payments"),
    "card_accounts": Spec(list, [], "payments"),
    "zarinpal_enabled": Spec(bool, False, "payments"),
    "zarinpal_merchant": Spec(str, "", "payments", secret=True),
    "zarinpal_sandbox": Spec(bool, False, "payments"),
    "stars_enabled": Spec(bool, False, "payments"),
    "stars_rate": Spec(int, 2_000, "payments"),
    "receipt_duplicate_block": Spec(bool, False, "payments"),
    # service lifecycle
    "renew_resets_usage": Spec(bool, True, "services"),
    "warn_remaining_percent": Spec(int, 10, "services"),
    "warn_days": Spec(int, 2, "services"),
    "delete_expired_after_days": Spec(int, 7, "services"),
    "usage_sync_minutes": Spec(int, 10, "services"),
    "extras_enabled": Spec(bool, True, "services"),
    "extra_gb_price": Spec(int, 3_000, "services"),
    "extra_day_price": Spec(int, 2_000, "services"),
    "upgrade_enabled": Spec(bool, True, "services"),
    "username_method": Spec(str, "telegram_id", "services"),
    "username_prefix": Spec(str, "", "services"),
    # resellers
    "reseller_applications_enabled": Spec(bool, True, "resellers"),
    # reports
    "daily_report_hour": Spec(int, 23, "reports"),
    "log_retention_days": Spec(int, 30, "reports"),
}

_TTL = 15.0
_cache: dict[int, tuple[float, dict[str, Any]]] = {}


class SettingError(ValueError):
    pass


def defaults() -> dict[str, Any]:
    return {
        key: (list(spec.default) if isinstance(spec.default, list) else spec.default)
        for key, spec in SPECS.items()
    }


async def load(session: AsyncSession, shop_id: int, *, fresh: bool = False) -> dict[str, Any]:
    cached = _cache.get(shop_id)
    if cached and not fresh and cached[0] > time.monotonic():
        return cached[1]
    data = defaults()
    rows = await session.scalars(select(ShopSetting).where(ShopSetting.shop_id == shop_id))
    for row in rows:
        spec = SPECS.get(row.key)
        if spec is None:
            continue
        if spec.secret and isinstance(row.value, dict) and "enc" in row.value:
            data[row.key] = secretbox.decrypt("payment", row.value["enc"])
        else:
            data[row.key] = row.value
    _cache[shop_id] = (time.monotonic() + _TTL, data)
    return data


async def get(session: AsyncSession, shop_id: int, key: str) -> Any:
    if key not in SPECS:
        raise SettingError(f"unknown setting {key}")
    return (await load(session, shop_id))[key]


def coerce(key: str, value: Any) -> Any:
    spec = SPECS.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key}")
    if value is None:
        return None
    if spec.kind is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "on", "yes"}:
            return True
        if text in {"0", "false", "off", "no"}:
            return False
        raise SettingError(f"{key} expects true/false")
    if spec.kind is int:
        if isinstance(value, bool):
            raise SettingError(f"{key} expects a number")
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise SettingError(f"{key} expects a number") from exc
    if spec.kind is list:
        if not isinstance(value, list):
            raise SettingError(f"{key} expects a list")
        return value
    return str(value)


async def set_many(session: AsyncSession, shop_id: int, values: dict[str, Any]) -> dict[str, Any]:
    clean = {key: coerce(key, value) for key, value in values.items()}
    for key, value in clean.items():
        stored = value
        if SPECS[key].secret and value:
            # Payment credentials never sit in the settings table in clear text (SEC-09).
            stored = {"enc": secretbox.encrypt("payment", str(value))}
        stmt = insert(ShopSetting).values(shop_id=shop_id, key=key, value=stored)
        stmt = stmt.on_conflict_do_update(index_elements=["shop_id", "key"], set_={"value": stored})
        await session.execute(stmt)
    invalidate(shop_id)
    return clean


def invalidate(shop_id: int | None = None) -> None:
    if shop_id is None:
        _cache.clear()
    else:
        _cache.pop(shop_id, None)


def public_view(data: dict[str, Any]) -> dict[str, Any]:
    """Settings safe to send to a browser: secret values are replaced by a set/unset flag."""
    view: dict[str, Any] = {}
    for key, value in data.items():
        spec = SPECS[key]
        view[key] = {"set": bool(value)} if spec.secret else value
    return view
