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
    "referral_min_order": Spec(int, 0, "growth"),
    "referral_daily_max": Spec(int, 0, "growth"),
    "referral_monthly_max": Spec(int, 0, "growth"),
    "referral_every_purchase": Spec(bool, False, "growth"),
    "referral_repeat_percent": Spec(int, None, "growth"),  # None: the same as referral_percent
    "referral_banner_required": Spec(bool, False, "growth"),
    "renew_cashback_percent": Spec(int, 0, "growth"),
    "start_gift_amount": Spec(int, 0, "growth"),
    "start_gift_referred_only": Spec(bool, True, "growth"),
    "loyalty_enabled": Spec(bool, False, "growth"),
    "loyalty_tiers": Spec(list, [], "growth"),
    "wheel_enabled": Spec(bool, False, "growth"),
    "wheel_every_hours": Spec(int, 24, "growth"),
    "wheel_buyers_only": Spec(bool, True, "growth"),
    "wheel_prizes": Spec(
        list,
        [
            {"label": "5,000", "kind": "wallet", "value": 5_000, "weight": 30},
            {"label": "10%", "kind": "discount", "value": 10, "weight": 20},
            {"label": "20,000", "kind": "wallet", "value": 20_000, "weight": 5},
            {"label": "-", "kind": "nothing", "value": 0, "weight": 45},
        ],
        "growth",
    ),
    "lottery_enabled": Spec(bool, False, "growth"),
    "lottery_include_agents": Spec(bool, True, "growth"),
    "dice_enabled": Spec(bool, False, "growth"),
    "trust_channel_id": Spec(int, None, "growth"),
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
    "card_auto_approve": Spec(bool, False, "payments"),
    "card_auto_max_amount": Spec(int, 300_000, "payments"),
    "card_auto_min_history": Spec(int, 3, "payments"),
    "card_auto_approve_after_minutes": Spec(int, 0, "payments"),
    "card_pv_mode": Spec(bool, False, "payments"),
    "card_pv_username": Spec(str, "", "payments"),
    "card_first_pay_only": Spec(bool, False, "payments"),
    "card_auto_minutes": Spec(int, 60, "payments"),
    "card_pick": Spec(str, "all", "payments"),
    "card_random_tail": Spec(bool, False, "payments"),
    "card_verify_enabled": Spec(bool, False, "payments"),
    "card_verify_min_amount": Spec(int, 0, "payments"),
    "topup_bonus_rules": Spec(list, [], "payments"),
    "wallet_transfer_enabled": Spec(bool, False, "payments"),
    "wallet_transfer_min": Spec(int, 10_000, "payments"),
    "wallet_transfer_daily_max": Spec(int, 5_000_000, "payments"),
    "nowpayments_enabled": Spec(bool, False, "payments"),
    "nowpayments_api_key": Spec(str, "", "payments", secret=True),
    "nowpayments_ipn_secret": Spec(str, "", "payments", secret=True),
    "plisio_enabled": Spec(bool, False, "payments"),
    "plisio_api_key": Spec(str, "", "payments", secret=True),
    # Telegram Business automation (BOT-07)
    "business_enabled": Spec(bool, False, "business"),
    "business_reply": Spec(str, "", "business"),
    "business_reply_every_hours": Spec(int, 12, "business"),
    "business_quiet_minutes": Spec(int, 30, "business"),
    "business_rules": Spec(list, [], "business"),
    # a reseller's own bot (TEN-04); set on that bot's shop
    "resale_markup_percent": Spec(int, 20, "resale"),
    "resale_prices": Spec(dict, {}, "resale"),
    "resale_blocked": Spec(bool, False, "resale"),  # stopped by the shop's staff: only they restart it
    "miniapp_enabled": Spec(bool, True, "appearance"),
    # guest purchase on the web (CAT-08)
    "guest_buy_enabled": Spec(bool, False, "guest"),
    "guest_plan_ids": Spec(list, [], "guest"),
    # more Iranian gateways (PAY-08, PAY-09)
    "aqayepardakht_enabled": Spec(bool, False, "gateways"),
    "aqayepardakht_pin": Spec(str, "", "gateways", secret=True),
    "tetra98_enabled": Spec(bool, False, "gateways"),
    "tetra98_api_key": Spec(str, "", "gateways", secret=True),
    "variza_enabled": Spec(bool, False, "gateways"),
    "variza_api_token": Spec(str, "", "gateways", secret=True),
    "variza_webhook_secret": Spec(str, "", "gateways", secret=True),
    "abangateway_enabled": Spec(bool, False, "gateways"),
    "abangateway_url": Spec(str, "", "gateways"),
    "abangateway_api_key": Spec(str, "", "gateways", secret=True),
    "abangateway_rial": Spec(bool, True, "gateways"),
    "blupal_enabled": Spec(bool, False, "gateways"),
    "blupal_api_key": Spec(str, "", "gateways", secret=True),
    "tronado_enabled": Spec(bool, False, "gateways"),
    "tronado_api_key": Spec(str, "", "gateways", secret=True),
    "tronado_ipn_key": Spec(str, "", "gateways", secret=True),
    "tronado_wallet": Spec(str, "", "gateways"),
    "tronado_wage_percent": Spec(int, 0, "gateways"),
    "cubepay_enabled": Spec(bool, False, "gateways"),
    "cubepay_api_key": Spec(str, "", "gateways", secret=True),
    "cubepay_fee_percent": Spec(int, 0, "gateways"),
    "atlaspay_enabled": Spec(bool, False, "gateways"),
    "atlaspay_api_key": Spec(str, "", "gateways", secret=True),
    "tonpay_enabled": Spec(bool, False, "gateways"),
    "tonpay_api_key": Spec(str, "", "gateways", secret=True),
    "tonpay_webhook_secret": Spec(str, "", "gateways", secret=True),
    # crypto (PAY-11, PAY-13)
    "crypto_enabled": Spec(bool, False, "crypto"),
    "crypto_networks": Spec(list, ["usdt_trc20"], "crypto"),
    "tron_address": Spec(str, "", "crypto"),
    "ton_address": Spec(str, "", "crypto"),
    "trongrid_api_key": Spec(str, "", "crypto", secret=True),
    "toncenter_api_key": Spec(str, "", "crypto", secret=True),
    "fx_source": Spec(str, "nobitex", "crypto"),
    "usdt_rate": Spec(int, 0, "crypto"),
    "trx_rate": Spec(int, 0, "crypto"),
    "ton_rate": Spec(int, 0, "crypto"),
    "fx_markup_percent": Spec(int, 0, "crypto"),
    # service lifecycle
    "renew_resets_usage": Spec(bool, True, "services"),
    "renew_mode": Spec(str, "", "services"),  # "" follows renew_resets_usage (SVC-13)
    "warn_remaining_percent": Spec(int, 10, "services"),
    "warn_days": Spec(int, 2, "services"),
    "delete_expired_after_days": Spec(int, 7, "services"),
    "usage_sync_minutes": Spec(int, 10, "services"),
    "extras_enabled": Spec(bool, True, "services"),
    "extra_gb_price": Spec(int, 3_000, "services"),
    "extra_day_price": Spec(int, 2_000, "services"),
    "extra_price_roles": Spec(dict, {}, "services"),
    "upgrade_enabled": Spec(bool, True, "services"),
    "renew_queue_when_active": Spec(bool, False, "services"),
    "ban_disables_services": Spec(bool, True, "services"),
    "branded_sub_links": Spec(bool, True, "services"),
    "app_links": Spec(list, [], "services"),
    "move_enabled": Spec(bool, True, "services"),
    "move_price": Spec(int, 0, "services"),
    "move_free_limit": Spec(int, 0, "services"),
    "self_delete_enabled": Spec(bool, False, "services"),
    "self_delete_refund_percent": Spec(int, 80, "services"),
    "ip_notice_enabled": Spec(bool, True, "services"),
    "info_card_enabled": Spec(bool, True, "services"),
    "info_card_theme": Spec(str, "midnight", "services"),
    "info_card_qr": Spec(bool, True, "services"),
    # custom purchase: the customer picks data and days (CAT-03)
    "custom_buy_enabled": Spec(bool, False, "custom"),
    "custom_panel_id": Spec(int, None, "custom"),
    "custom_location_id": Spec(int, None, "custom"),
    "custom_gb_price": Spec(int, 2_500, "custom"),
    "custom_day_price": Spec(int, 1_500, "custom"),
    "custom_base_price": Spec(int, 0, "custom"),
    "custom_min_gb": Spec(int, 10, "custom"),
    "custom_max_gb": Spec(int, 500, "custom"),
    "custom_min_days": Spec(int, 7, "custom"),
    "custom_max_days": Spec(int, 180, "custom"),
    # bot appearance (BOT-02, BOT-03, BOT-05)
    "menu_layout": Spec(list, [], "appearance"),
    "button_styles": Spec(dict, {}, "appearance"),
    "premium_emoji_enabled": Spec(bool, False, "appearance"),
    "premium_emojis": Spec(dict, {}, "appearance"),  # emoji -> custom emoji id (BOT-10)
    "start_media": Spec(dict, {}, "appearance"),  # {"type": sticker|animation|photo, "file_id"} (BOT-11)
    "buy_banner_file_id": Spec(str, "", "appearance"),
    "menu_mode": Spec(str, "reply", "appearance"),  # reply | inline | miniapp (BOT-12)
    "help_items": Spec(list, [], "appearance"),
    "download_apps": Spec(list, [], "appearance"),
    "app_cache_chat_id": Spec(int, None, "appearance"),
    "username_method": Spec(str, "telegram_id", "services"),
    "username_prefix": Spec(str, "", "services"),
    "custom_username_enabled": Spec(bool, False, "services"),
    # resellers
    "reseller_applications_enabled": Spec(bool, True, "resellers"),
    "bulk_buy_enabled": Spec(bool, True, "resellers"),
    "bulk_buy_max": Spec(int, 20, "resellers"),
    "bulk_buy_resellers_only": Spec(bool, True, "resellers"),
    "reseller_web_access": Spec(bool, True, "resellers"),
    "reseller_membership_price": Spec(int, 0, "resellers"),
    "reseller_membership_group_id": Spec(int, None, "resellers"),
    "reseller_membership_days": Spec(int, 30, "resellers"),
    "reseller_bot_open": Spec(bool, False, "resellers"),
    "panel_accounts_enabled": Spec(bool, False, "resellers"),
    "panel_accounts_resellers_only": Spec(bool, False, "resellers"),
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
        key: (
            list(spec.default)
            if isinstance(spec.default, list)
            else dict(spec.default)
            if isinstance(spec.default, dict)
            else spec.default
        )
        for key, spec in SPECS.items()
    }


async def load(session: AsyncSession, shop_id: int, *, fresh: bool = False) -> dict[str, Any]:
    cached = _cache.get(shop_id)
    if cached and not fresh and cached[0] > time.monotonic():
        return cached[1]
    data = defaults()
    # Plain columns, not ORM rows: set_many writes with an upsert the identity map never sees.
    rows = await session.execute(
        select(ShopSetting.key, ShopSetting.value).where(ShopSetting.shop_id == shop_id)
    )
    for key, value in rows.all():
        spec = SPECS.get(key)
        if spec is None:
            continue
        if spec.secret and isinstance(value, dict) and "enc" in value:
            data[key] = secretbox.decrypt("payment", value["enc"])
        else:
            data[key] = value
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
    if spec.kind is dict:
        if not isinstance(value, dict):
            raise SettingError(f"{key} expects an object")
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
