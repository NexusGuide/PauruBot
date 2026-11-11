"""Per-shop runtime settings with typed defaults, stored in `shop_settings`."""

from __future__ import annotations

import time
import uuid
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
    # entry: before using the bot at all; purchase: only before a new purchase.
    "phone_when": Spec(str, "entry", "general"),
    "support_username": Spec(str, "", "general"),
    "terms_required": Spec(bool, False, "general"),
    "terms_version": Spec(int, 1, "general"),
    # entry: before using the bot at all; purchase: only before a new purchase.
    "terms_when": Spec(str, "entry", "general"),
    # typed second step for destructive staff actions on the web (F-22)
    "typed_confirm_enabled": Spec(bool, False, "general"),
    "typed_confirm_refund_min": Spec(int, 1_000_000, "general"),
    # trial (CAT-04)
    "trial_enabled": Spec(bool, True, "trial"),
    "trial_panel_id": Spec(int, None, "trial"),
    "trial_days": Spec(int, 1, "trial"),
    "trial_gb": Spec(int, 1, "trial"),
    "trial_generation": Spec(int, 1, "trial"),
    "trial_daily_quota": Spec(int, 0, "trial"),
    "trial_requires_phone": Spec(bool, False, "trial"),
    # Minutes a new account waits before its free trial; 0 is off (REF-04).
    "trial_min_account_minutes": Spec(int, 0, "trial"),
    # referral (GRW-01)
    "referral_enabled": Spec(bool, True, "growth"),
    # A newcomer must come with an inviter (a link or the inviter's code) before using the shop.
    "referral_required": Spec(bool, False, "growth"),
    "referral_percent": Spec(int, 10, "growth"),
    "referral_min_order": Spec(int, 0, "growth"),
    "referral_daily_max": Spec(int, 0, "growth"),
    "referral_monthly_max": Spec(int, 0, "growth"),
    # Foxima parity, against fake invites: an invite counts only once the friend shared a phone
    # number, and only after their account is this many hours old (0: no wait).
    "referral_require_phone": Spec(bool, False, "growth"),
    "referral_min_account_hours": Spec(int, 0, "growth"),
    "referral_every_purchase": Spec(bool, False, "growth"),
    "referral_repeat_percent": Spec(int, None, "growth"),  # None: the same as referral_percent
    # A free config for every N counted invites, instead of or beside the cash reward
    # (see paurubot.core.referral_rewards). Mode: cash | config | both.
    "referral_reward_mode": Spec(str, "cash", "growth"),
    "referral_config_counts": Spec(str, "purchase", "growth"),  # purchase | join
    "referral_config_every": Spec(int, 5, "growth"),
    "referral_config_source": Spec(str, "plan", "growth"),  # plan | custom
    "referral_config_plan_id": Spec(int, None, "growth"),
    "referral_config_gb": Spec(int, 5, "growth"),
    "referral_config_days": Spec(int, 30, "growth"),
    "referral_config_panel_id": Spec(int, None, "growth"),
    "start_gift_amount": Spec(int, 0, "growth"),
    "start_gift_referred_only": Spec(bool, True, "growth"),
    # A time-limited discount for customers on the whole catalog, or only some categories
    # (ClockBot parity). 0 is off; an empty end means until turned off.
    "catalog_discount_percent": Spec(int, 0, "growth"),
    "catalog_discount_until": Spec(str, "", "growth"),
    "catalog_discount_category_ids": Spec(list, [], "growth"),
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
    "trust_channel_id": Spec(int, None, "growth"),
    # payments
    # Amounts offered as buttons on a wallet top-up; empty: the built-in list.
    "wallet_topup_presets": Spec(list, [], "payments"),
    "min_topup": Spec(int, 20_000, "payments"),
    # Foxima parity: per payment method, a minimum and a maximum amount and a help text shown
    # before paying: [{"provider": "zarinpal", "min": 0, "max": 0, "help": ""}] (0: no own limit).
    "payment_method_rules": Spec(list, [], "payments"),
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
    "card_pick": Spec(str, "all", "payments"),
    "card_random_tail": Spec(bool, False, "payments"),
    # Bank deposit SMS posted by a forwarder app approve card payments (REF-02).
    "card_sms_enabled": Spec(bool, False, "payments"),
    "card_sms_secret": Spec(str, "", "payments", secret=True),
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
    "resale_blocked": Spec(bool, False, "resale"),
    # A dedicated own-panel bot (OPB), kept by the bot itself and the main shop's staff, never on
    # the settings page: why it stopped selling ("" while it sells) and a manual pause by staff.
    "own_sales_paused": Spec(str, "", "own"),
    "own_sales_hold": Spec(bool, False, "own"),  # stopped by the shop's staff: only they restart it
    # Set on load for a dedicated bot's shop (never stored): a selling-only bot.
    "own_bot": Spec(bool, False, "own"),
    # Until when the shop's staff let this dedicated bot sell (ISO time); "" has no end.
    "own_bot_expires_at": Spec(str, "", "own"),
    "own_bot_expiry_warned": Spec(str, "", "own"),  # the end date already warned about
    # Set in the main shop: what a reseller pays from its wallet per trial its own bot gives (TEN-18).
    "resale_trial_fee": Spec(int, 2_000, "resale"),
    "miniapp_enabled": Spec(bool, True, "appearance"),
    # The shop's own look in the Mini App (REF-08): a preset colour, the bot's photo, a notice.
    "miniapp_accent": Spec(str, "", "appearance"),
    "miniapp_logo": Spec(bool, False, "appearance"),
    "miniapp_notice": Spec(str, "", "appearance"),
    # guest purchase on the web (CAT-08)
    "guest_buy_enabled": Spec(bool, False, "guest"),
    "guest_plan_ids": Spec(list, [], "guest"),
    # Admin account plans on the web purchase page too (when admin accounts are sold to everyone).
    "guest_accounts_enabled": Spec(bool, True, "guest"),
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
    "tonpay_enabled": Spec(bool, False, "gateways"),
    "tonpay_api_key": Spec(str, "", "gateways", secret=True),
    # Custom checkout is a distinct credential and explicit owner-approved policy.
    "tonpay_custom_enabled": Spec(bool, False, "gateways"),
    "tonpay_custom_api_key": Spec(str, "", "gateways", secret=True),
    "tonpay_custom_credential_epoch": Spec(str, "", "gateways"),
    "tonpay_custom_policy_approved": Spec(bool, False, "gateways"),
    "tonpay_custom_confirm_enabled": Spec(bool, False, "gateways"),
    "tonpay_custom_receipt_wait": Spec(int, 0, "gateways"),
    "tonpay_custom_card_cooldown": Spec(int, 0, "gateways"),
    "tonpay_custom_review_window": Spec(int, 0, "gateways"),
    "atlaspay_enabled": Spec(bool, False, "gateways"),
    "atlaspay_api_key": Spec(str, "", "gateways", secret=True),
    # crypto (PAY-11, PAY-13)
    "crypto_enabled": Spec(bool, False, "crypto"),
    "crypto_networks": Spec(list, ["usdt_trc20"], "crypto"),
    "tron_address": Spec(str, "", "crypto"),
    "ton_address": Spec(str, "", "crypto"),
    "trongrid_api_key": Spec(str, "", "crypto", secret=True),
    "toncenter_api_key": Spec(str, "", "crypto", secret=True),
    "fx_source": Spec(str, "nobitex", "crypto"),
    # fx_source "api": the shop's own JSON rate API (ClockBot parity); {symbol} is usdt/trx/ton.
    "fx_api_url": Spec(str, "", "crypto"),
    "fx_api_path": Spec(str, "", "crypto"),
    "fx_api_divisor": Spec(int, 1, "crypto"),
    "usdt_rate": Spec(int, 0, "crypto"),
    "trx_rate": Spec(int, 0, "crypto"),
    "ton_rate": Spec(int, 0, "crypto"),
    "fx_markup_percent": Spec(int, 0, "crypto"),
    # service lifecycle
    "renew_resets_usage": Spec(bool, True, "services"),
    "renew_mode": Spec(str, "", "services"),  # "" follows renew_resets_usage (SVC-13)
    "warn_remaining_percent": Spec(int, 10, "services"),
    # Tell staff when a panel's services reach this percent of its capacity; 0 is off.
    "capacity_warn_percent": Spec(int, 0, "services"),
    # Local hours (0-23) when customer alerts wait for the morning; off unless both are set.
    "alerts_quiet_start": Spec(int, None, "services"),
    # Hours after purchase to remind a customer whose service was never used; 0 is off.
    "first_use_nudge_hours": Spec(int, 0, "services"),
    "alerts_quiet_end": Spec(int, None, "services"),
    "warn_days": Spec(int, 2, "services"),
    "delete_expired_after_days": Spec(int, 7, "services"),
    "usage_sync_minutes": Spec(int, 10, "services"),
    "extras_enabled": Spec(bool, True, "services"),
    # Ready-made extra packages (ClockBot parity): [{"unit": "gb"|"days", "amount", "price"}].
    "extra_packages": Spec(list, [], "services"),
    "extra_gb_price": Spec(int, 3_000, "services"),
    "extra_day_price": Spec(int, 2_000, "services"),
    # How much extra data / time one order may buy; 0 is no bound of the shop's own.
    "extra_gb_min": Spec(int, 0, "services"),
    "extra_gb_max": Spec(int, 0, "services"),
    "extra_day_min": Spec(int, 0, "services"),
    "extra_day_max": Spec(int, 0, "services"),
    "upgrade_enabled": Spec(bool, True, "services"),
    "renew_queue_when_active": Spec(bool, False, "services"),
    "ban_disables_services": Spec(bool, True, "services"),
    "branded_sub_links": Spec(bool, True, "services"),
    "app_links": Spec(list, [], "services"),
    "move_enabled": Spec(bool, True, "services"),
    "move_price": Spec(int, 0, "services"),
    "self_delete_enabled": Spec(bool, False, "services"),
    "self_delete_refund_percent": Spec(int, 80, "services"),
    "ip_notice_enabled": Spec(bool, True, "services"),
    # Shop-wide caps for customers (resellers follow their own profile's caps); 0 is no cap.
    "max_active_services": Spec(int, 0, "services"),
    # Customers add a service they already have on the shop's panels; at once when the panel
    # user's note names them (auto), else after staff approve.
    "service_import_enabled": Spec(bool, False, "services"),
    "service_import_auto": Spec(bool, True, "services"),
    # Panels a previous service may come from; empty: all of the shop's panels.
    "service_import_panel_ids": Spec(list, [], "services"),
    "max_daily_purchases": Spec(int, 0, "services"),
    "info_card_enabled": Spec(bool, True, "services"),
    "info_card_theme": Spec(str, "midnight", "services"),
    "info_card_qr": Spec(bool, True, "services"),
    # ClockBot parity: the QR photo after delivery can be turned off, and can carry the link.
    "qr_send_enabled": Spec(bool, True, "services"),
    "qr_show_link": Spec(bool, False, "services"),
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
    # Keep one home menu per chat: a new one deletes the previous (P3, after PGClockBot).
    "menu_cleanup": Spec(bool, True, "appearance"),
    # Connection guides per device with a picture or video (ClockBot parity):
    # [{"title", "text", "media_url"?, "media_type": photo|video, "link"?}]
    "connection_guides": Spec(list, [], "appearance"),
    "help_items": Spec(list, [], "appearance"),
    "download_apps": Spec(list, [], "appearance"),
    "app_cache_chat_id": Spec(int, None, "appearance"),
    "username_method": Spec(str, "telegram_id", "services"),
    "username_prefix": Spec(str, "", "services"),
    # A fixed suffix after every new panel username (ClockBot parity); empty is none.
    "username_suffix": Spec(str, "", "services"),
    "custom_username_enabled": Spec(bool, False, "services"),
    # resellers
    "reseller_applications_enabled": Spec(bool, True, "resellers"),
    "bulk_buy_enabled": Spec(bool, True, "resellers"),
    "bulk_buy_max": Spec(int, 20, "resellers"),
    "bulk_buy_resellers_only": Spec(bool, True, "resellers"),
    # Quantity discounts on a bulk buy (ClockBot's wholesale tiers): [{"min": 5, "percent": 10}].
    "bulk_buy_tiers": Spec(list, [], "resellers"),
    "reseller_web_access": Spec(bool, True, "resellers"),
    "reseller_membership_price": Spec(int, 0, "resellers"),
    "reseller_membership_group_id": Spec(int, None, "resellers"),
    "reseller_membership_days": Spec(int, 30, "resellers"),
    "reseller_bot_open": Spec(bool, False, "resellers"),
    # Separate terms a reseller accepts before buying (text: reseller_terms_text); a new
    # version asks again.
    "reseller_terms_required": Spec(bool, False, "resellers"),
    "reseller_terms_version": Spec(int, 1, "resellers"),
    "panel_accounts_enabled": Spec(bool, False, "resellers"),
    "panel_accounts_resellers_only": Spec(bool, False, "resellers"),
    # How much extra traffic or time an admin account owner may buy in one order.
    "panel_accounts_gb_min": Spec(int, 1, "resellers"),
    "panel_accounts_gb_max": Spec(int, 1000, "resellers"),
    "panel_accounts_days_min": Spec(int, 1, "resellers"),
    "panel_accounts_days_max": Spec(int, 365, "resellers"),
    # reports
    # Admin alerts turned off: "group:<report topic>" or "dm:<staff event>" (settings › alerts).
    "alerts_muted": Spec(list, [], "reports"),
    # Answers to free messages that contain a keyword: [{"words": [...], "text": "..."}].
    "auto_replies": Spec(list, [], "general"),
    "daily_report_hour": Spec(int, 23, "reports"),
    "daily_report_enabled": Spec(bool, True, "reports"),
    # Each reseller gets its own nightly figures in the bot (ClockBot parity).
    "reseller_daily_report": Spec(bool, False, "reports"),
    # Which parts of the nightly report are sent, in the report's own order.
    "daily_report_sections": Spec(list, ["summary", "money", "panels"], "reports"),
    "log_retention_days": Spec(int, 30, "reports"),
    # backups: server-wide, read from the root shop and edited on the Backups page only.
    # An unset value falls back to the server's BACKUP_* environment setting.
    "backup_auto_enabled": Spec(bool, True, "backups"),
    "backup_every_hours": Spec(int, 24, "backups"),
    "backup_hour": Spec(int, None, "backups"),
    "backup_keep": Spec(int, None, "backups"),
    "backup_send_telegram": Spec(bool, None, "backups"),
    # A separate bot and chats for backups (ClockBot parity); unset: the shop's bot and staff.
    "backup_bot_token": Spec(str, "", "backups", secret=True),
    "backup_chat_ids": Spec(list, [], "backups"),
}

_TTL = 15.0
_cache: dict[int, tuple[float, dict[str, Any]]] = {}


class SettingError(ValueError):
    pass


DAILY_SECTIONS = ("summary", "money", "panels")
# Topics of the report group (SUP-03) and the events sent to staff chats; each can be muted.
REPORT_TOPICS = (
    "orders",
    "payments",
    "users",
    "errors",
    "services",
    "reports",
    "backups",
    "resellers",
    "panels",
    "tickets",
    "trials",
    "referrals",
)
STAFF_EVENTS = (
    "receipt",
    "service_claim",
    "card_verify",
    "order_failed",
    "reseller_application",
    "panel",
    "lottery",
    "own_bot",
)
ALERT_KEYS = {f"group:{t}" for t in REPORT_TOPICS} | {f"dm:{e}" for e in STAFF_EVENTS}
MAX_AUTO_REPLIES, MAX_AUTO_WORDS, MAX_WORD, MAX_REPLY = 30, 12, 40, 2000


def _check_method_rules(value: list[Any]) -> None:
    from paurubot.payments.engine import PROVIDERS

    seen: set[str] = set()
    if len(value) > 20:
        raise SettingError("payment_method_rules_invalid")
    for rule in value:
        if not isinstance(rule, dict) or rule.get("provider") not in PROVIDERS or rule["provider"] in seen:
            raise SettingError("payment_method_rules_invalid")
        seen.add(rule["provider"])
        low, high, help_text = rule.get("min") or 0, rule.get("max") or 0, rule.get("help") or ""
        if (
            not isinstance(low, int)
            or not isinstance(high, int)
            or isinstance(low, bool)
            or isinstance(high, bool)
            or not 0 <= low <= 10**12
            or not 0 <= high <= 10**12
            or (low and high and low > high)
            or not isinstance(help_text, str)
            or len(help_text) > 2000
        ):
            raise SettingError("payment_method_rules_invalid")


def _check_guides(value: list[Any]) -> None:
    if len(value) > 12:
        raise SettingError("connection_guides_invalid")
    for guide in value:
        if not isinstance(guide, dict):
            raise SettingError("connection_guides_invalid")
        title, text = guide.get("title"), guide.get("text")
        media, link = str(guide.get("media_url") or ""), str(guide.get("link") or "")
        if (
            not isinstance(title, str)
            or not 0 < len(title.strip()) <= 40
            or not isinstance(text, str)
            or not 0 < len(text.strip()) <= 3000
            or (media and not media.startswith("https://"))
            or (link and not link.startswith("https://"))
            or guide.get("media_type", "photo") not in {"photo", "video", "", None}
        ):
            raise SettingError("connection_guides_invalid")


def _check_auto_replies(value: list[Any]) -> None:
    if len(value) > MAX_AUTO_REPLIES:
        raise SettingError("auto_replies_invalid")
    for rule in value:
        words = rule.get("words") if isinstance(rule, dict) else None
        text = rule.get("text") if isinstance(rule, dict) else None
        if (
            not isinstance(words, list)
            or not 0 < len(words) <= MAX_AUTO_WORDS
            or not all(isinstance(w, str) and 0 < len(w.strip()) <= MAX_WORD for w in words)
            or not isinstance(text, str)
            or not 0 < len(text.strip()) <= MAX_REPLY
        ):
            raise SettingError("auto_replies_invalid")


def _check(key: str, value: Any) -> None:
    """Values a type alone cannot rule out."""
    if value is None:
        return
    if key == "bulk_buy_tiers" and not all(
        isinstance(tier, dict)
        and isinstance(tier.get("min"), int)
        and isinstance(tier.get("percent"), int)
        and 2 <= tier["min"] <= 1000
        and 1 <= tier["percent"] <= 90
        for tier in value
    ):
        raise SettingError("bulk_buy_tiers_invalid")
    if key == "catalog_discount_percent" and not 0 <= value <= 90:
        raise SettingError("catalog_discount_percent_invalid")
    if key == "catalog_discount_until" and value:
        from datetime import datetime

        try:
            datetime.fromisoformat(str(value))
        except ValueError:
            raise SettingError("catalog_discount_until_invalid") from None
    if key == "catalog_discount_category_ids" and not all(isinstance(v, int) for v in value):
        raise SettingError("catalog_discount_category_ids_invalid")
    if key == "wallet_topup_presets" and (
        len(value) > 8
        or not all(isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 10**10 for v in value)
    ):
        raise SettingError("wallet_topup_presets_invalid")
    if key == "extra_packages" and (
        len(value) > 12
        or not all(
            isinstance(p, dict)
            and p.get("unit") in {"gb", "days"}
            and isinstance(p.get("amount"), int)
            and isinstance(p.get("price"), int)
            and 0 < p["amount"] <= 100_000
            and 0 <= p["price"] <= 10**10
            for p in value
        )
    ):
        raise SettingError("extra_packages_invalid")
    if key == "connection_guides":
        _check_guides(value)
    if key == "payment_method_rules":
        _check_method_rules(value)
    if key == "auto_replies":
        _check_auto_replies(value)
    if key in {"terms_when", "phone_when"} and value not in {"entry", "purchase"}:
        raise SettingError(f"{key}_invalid")
    if key == "alerts_muted" and not set(value) <= ALERT_KEYS:
        raise SettingError("alerts_muted_invalid")
    if key == "daily_report_sections" and not set(value) <= set(DAILY_SECTIONS):
        raise SettingError("daily_report_sections_invalid")
    if key in {"alerts_quiet_start", "alerts_quiet_end"} and not 0 <= value <= 23:
        raise SettingError(f"{key} must be an hour from 0 to 23")
    if key in {"max_active_services", "max_daily_purchases"} and value < 0:
        raise SettingError(f"{key} must not be negative")


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
    await _inherit(session, shop_id, data, fresh)
    _cache[shop_id] = (time.monotonic() + _TTL, data)
    return data


# A dedicated bot on a reseller's own panel follows the main shop for these (OPB).
INHERITED_BY_OWN_BOTS = ("languages",)


async def _inherit(session: AsyncSession, shop_id: int, data: dict[str, Any], fresh: bool) -> None:
    from paurubot.db.models import Shop

    shop = await session.get(Shop, shop_id)
    if shop is None or shop.own_account_id is None or shop.parent_id is None:
        return
    parent = await load(session, shop.parent_id, fresh=fresh)
    for key in INHERITED_BY_OWN_BOTS:
        data[key] = parent[key]
    data["own_bot"] = True


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
        _check(key, value)
    if any(key.startswith("tonpay_custom_") for key in clean):
        from paurubot.payments.base import PaymentError
        from paurubot.payments.tonpay_custom_runtime import configuration

        for key in (
            "tonpay_custom_receipt_wait",
            "tonpay_custom_card_cooldown",
            "tonpay_custom_review_window",
        ):
            if key in values:
                raw = values[key]
                if isinstance(raw, bool) or not (
                    isinstance(raw, int) or isinstance(raw, str) and raw.isascii() and raw.isdigit()
                ):
                    raise SettingError("tonpay_custom_policy_invalid")
        current = await load(session, shop_id, fresh=True)
        if "tonpay_custom_api_key" in clean and clean["tonpay_custom_api_key"] != current.get(
            "tonpay_custom_api_key"
        ):
            clean["tonpay_custom_credential_epoch"] = uuid.uuid4().hex
        merged = {**current, **clean}
        if merged.get("tonpay_custom_enabled"):
            if merged.get("tonpay_custom_confirm_enabled") is not True:
                raise SettingError("tonpay_custom_confirmation_required")
            try:
                configuration(shop_id, merged, int(merged["min_topup"]), int(merged["max_topup"]))
            except (PaymentError, TypeError, ValueError):
                raise SettingError("tonpay_custom_configuration_invalid") from None
    if "card_sms_secret" in clean or clean.get("card_sms_enabled"):
        # A guessable key would let anyone approve card payments (REF-02).
        secret = clean.get("card_sms_secret")
        if secret is not None and secret != "" and len(secret) < 24:
            raise SettingError("card_sms_secret_short")
        if secret is None and "card_sms_secret" not in clean:
            secret = (await load(session, shop_id, fresh=True)).get("card_sms_secret")
        enabled = clean.get("card_sms_enabled")
        if enabled is None:
            enabled = (await load(session, shop_id, fresh=True)).get("card_sms_enabled")
        if enabled and not secret:
            raise SettingError("card_sms_secret_required")
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
