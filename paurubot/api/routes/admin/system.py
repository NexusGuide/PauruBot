"""Shop settings, bot texts, channels, support contacts, audit trail, jobs and sales reports."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Date, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import i18n, kv
from paurubot.api.admin_common import Page, owned, page_params, paginate, plain, reseller_scope
from paurubot.api.channels import require_channel, resolve_channel
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import channels as channel_rules
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    BotText,
    ForcedChannel,
    Job,
    JobStatus,
    LogChannel,
    Order,
    OrderStatus,
    Panel,
    Payment,
    PaymentStatus,
    Plan,
    SupportContact,
    TrustChannel,
)
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])

LOG_TOPICS = ("all", *shop_settings.REPORT_TOPICS)
CHANNEL_FIELDS = ["id", "chat_id", "title", "url", "is_active", "sort"]
TRUST_FIELDS = ["id", "chat_id", "title", "url", "kinds", "is_active", "sort"]
LOG_FIELDS = ["id", "topic", "chat_id", "thread_id"]
CONTACT_FIELDS = ["id", "title", "url", "sort"]
AUDIT_FIELDS = [
    "id",
    "actor_type",
    "actor_id",
    "action",
    "entity",
    "entity_id",
    "before",
    "after",
    "ip",
    "created_at",
]
JOB_FIELDS = [
    "id",
    "queue",
    "kind",
    "status",
    "attempts",
    "max_attempts",
    "run_at",
    "last_error",
    "created_at",
    "finished_at",
]


# ---------------------------------------------------------------- settings


@router.get("/settings")
async def read_settings(
    ctx: Annotated[StaffContext, Depends(require("settings.read"))], db: DB
) -> dict[str, Any]:
    data = await shop_settings.load(db, ctx.shop_id, fresh=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    view = shop_settings.public_view(data)
    for key, spec in shop_settings.SPECS.items():
        if spec.group in {"backups", "own"}:
            continue  # server-wide, on the Backups page; a dedicated bot's state, on its customer
        groups.setdefault(spec.group, []).append(
            {"key": key, "type": spec.kind.__name__, "secret": spec.secret, "value": view[key]}
        )
    return {"groups": groups}


class SettingsBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _check_card_accounts(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 20:
        raise HTTPException(status_code=422, detail="card_accounts_invalid")
    for item in value:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="card_accounts_invalid")
        number = "".join(ch for ch in str(item.get("number", "")) if ch.isdigit())
        if len(number) not in {16, 24, 26} and not str(item.get("number", "")).upper().startswith("IR"):
            raise HTTPException(status_code=422, detail="card_number_invalid")


def _check_appearance(values: dict[str, Any]) -> None:
    from paurubot.bot import menu
    from paurubot.core import apps

    checks = {
        "menu_layout": menu.validate_layout,
        "button_styles": menu.validate_styles,
        "help_items": menu.validate_help,
        "download_apps": apps.validate,
    }
    for key, check in checks.items():
        if key in values:
            try:
                values[key] = check(values[key])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
    from paurubot.core import brand

    if "miniapp_accent" in values and values["miniapp_accent"] not in brand.ACCENTS:
        raise HTTPException(status_code=422, detail="miniapp_accent_invalid")
    if "miniapp_notice" in values:
        values["miniapp_notice"] = brand.clean_notice(values["miniapp_notice"])


def _check_custom(values: dict[str, Any]) -> None:
    for low, high in (("custom_min_gb", "custom_max_gb"), ("custom_min_days", "custom_max_days")):
        lo, hi = values.get(low), values.get(high)
        if lo is not None and (not isinstance(lo, int) or lo < 1):
            raise HTTPException(status_code=422, detail=f"{low}_invalid")
        if lo is not None and hi is not None and isinstance(hi, int) and hi < lo:
            raise HTTPException(status_code=422, detail=f"{high}_invalid")
    for key in ("custom_gb_price", "custom_day_price", "custom_base_price"):
        value = values.get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")


ACCOUNT_EXTRA_KEYS = (
    "panel_accounts_gb_min",
    "panel_accounts_gb_max",
    "panel_accounts_days_min",
    "panel_accounts_days_max",
)


async def _check_account_extras(db: AsyncSession, shop_id: int, values: dict[str, Any]) -> None:
    """Extra GB and days for admin accounts: 1 <= least <= most <= the hard cap, with the saved
    value standing in for the half of a pair not being changed."""
    from paurubot.core import panel_accounts

    if not any(key in values for key in ACCOUNT_EXTRA_KEYS):
        return
    current = await shop_settings.load(db, shop_id, fresh=True)
    merged: dict[str, int] = {}
    for key in ACCOUNT_EXTRA_KEYS:
        raw = values.get(key, current.get(key))
        if isinstance(raw, bool):
            raise HTTPException(status_code=422, detail="account_extras_invalid")
        try:
            merged[key] = int(str(raw).strip())
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="account_extras_invalid") from None
    for unit, cap in (("gb", panel_accounts.MAX_GB), ("days", panel_accounts.MAX_DAYS)):
        low, high = merged[f"panel_accounts_{unit}_min"], merged[f"panel_accounts_{unit}_max"]
        if not 1 <= low <= high <= cap:
            raise HTTPException(status_code=422, detail="account_extras_invalid")


def _check_growth(values: dict[str, Any]) -> None:
    from paurubot.core import growth

    for key, check in (("loyalty_tiers", growth.validate_tiers), ("wheel_prizes", growth.validate_prizes)):
        if key in values:
            try:
                values[key] = check(values[key])
            except growth.GrowthError as exc:
                raise HTTPException(status_code=422, detail=exc.key) from None
    hours = values.get("wheel_every_hours")
    if hours is not None and (not isinstance(hours, int) or not 1 <= hours <= 24 * 90):
        raise HTTPException(status_code=422, detail="wheel_every_hours_invalid")


def _check_crypto(values: dict[str, Any]) -> None:
    from paurubot.payments.chains import NETWORKS, valid_address

    networks = values.get("crypto_networks")
    if networks is not None and (not isinstance(networks, list) or set(networks) - set(NETWORKS)):
        raise HTTPException(status_code=422, detail="crypto_networks_invalid")
    for key, network in (("tron_address", "trx"), ("ton_address", "ton")):
        address = values.get(key)
        if address and not valid_address(network, str(address).strip()):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")
    if values.get("fx_api_url") and not str(values["fx_api_url"]).startswith("https://"):
        raise HTTPException(status_code=422, detail="fx_api_url_invalid")
    if values.get("fx_source") not in (None, "nobitex", "manual", "api"):
        raise HTTPException(status_code=422, detail="fx_source_invalid")


def _check_gateways(values: dict[str, Any]) -> None:
    from paurubot.payments.chains import valid_address
    from paurubot.security import ssrf

    url = values.get("abangateway_url")
    if url:
        try:
            ssrf.check_url_shape(str(url))
            ssrf.check_static(str(url))
        except ssrf.UnsafeUrl:
            raise HTTPException(status_code=422, detail="abangateway_url_invalid") from None
        if not str(url).startswith("https://"):
            raise HTTPException(status_code=422, detail="abangateway_url_invalid")
        values["abangateway_url"] = str(url).strip().rstrip("/")
    wallet = values.get("tronado_wallet")
    if wallet and not valid_address("trx", str(wallet).strip()):
        raise HTTPException(status_code=422, detail="tronado_wallet_invalid")
    for key in ("tronado_wage_percent", "cubepay_fee_percent"):
        value = values.get(key)
        if value is not None and (not isinstance(value, int) or not 0 <= value <= 100):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")


def _check_business(values: dict[str, Any]) -> None:
    from paurubot.core import business

    if "business_rules" in values:
        try:
            values["business_rules"] = business.validate_rules(values["business_rules"])
        except business.BusinessError as exc:
            raise HTTPException(status_code=422, detail=exc.key) from None
    reply = values.get("business_reply")
    if reply is not None and (not isinstance(reply, str) or len(reply) > business.MAX_REPLY):
        raise HTTPException(status_code=422, detail="business_reply_invalid")
    for key, low, high in (
        ("business_reply_every_hours", 1, 24 * 30),
        ("business_quiet_minutes", 0, 24 * 60),
    ):
        value = values.get(key)
        if value is not None and (not isinstance(value, int) or not low <= value <= high):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")


def _check_services(values: dict[str, Any]) -> None:
    from paurubot.bot.infocard import THEMES

    if values.get("info_card_theme") not in (None, *THEMES):
        raise HTTPException(status_code=422, detail="info_card_theme_invalid")
    plans = values.get("guest_plan_ids")
    if plans is not None:
        if not isinstance(plans, list) or len(plans) > 200 or not all(str(p).isdigit() for p in plans):
            raise HTTPException(status_code=422, detail="guest_plan_ids_invalid")
        values["guest_plan_ids"] = sorted({int(p) for p in plans})


@router.put("/settings")
async def write_settings(
    body: SettingsBody, ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, Any]:
    unknown = [k for k in body.values if k not in shop_settings.SPECS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown_settings:{','.join(sorted(unknown))}")
    if any(shop_settings.SPECS[k].group == "backups" for k in body.values):
        # Server-wide, so only through the Backups page and its own permission.
        raise HTTPException(status_code=422, detail="backup_settings_elsewhere")
    if any(shop_settings.SPECS[k].group == "own" for k in body.values):
        raise HTTPException(status_code=422, detail="own_bot_settings_elsewhere")
    if any(key.startswith("tonpay_custom_") for key in body.values) and not ctx.can("panels.secrets"):
        raise HTTPException(status_code=403, detail="secrets_need_owner")
    secret_keys = [k for k in body.values if shop_settings.SPECS[k].secret]
    if secret_keys and not ctx.can("panels.secrets"):
        # Payment merchant keys follow the same owner-only rule as panel credentials (AUTHZ rule 4).
        raise HTTPException(status_code=403, detail="secrets_need_owner")
    if "card_accounts" in body.values:
        _check_card_accounts(body.values["card_accounts"])
    if "topup_bonus_rules" in body.values:
        from paurubot.payments.bonus import BonusRuleError, validate_rules
        from paurubot.payments.engine import PROVIDERS

        try:
            body.values["topup_bonus_rules"] = validate_rules(
                body.values["topup_bonus_rules"], set(PROVIDERS)
            )
        except BonusRuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "menu_mode" in body.values and body.values["menu_mode"] not in ("reply", "inline", "miniapp"):
        raise HTTPException(status_code=422, detail="menu_mode_invalid")
    for key in ("premium_emojis", "start_media"):
        if key in body.values and not isinstance(body.values[key], dict):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")
    if "premium_emojis" in body.values:
        from paurubot.bot.premium_emoji import clean_map

        body.values["premium_emojis"] = clean_map(body.values["premium_emojis"])
    if "renew_mode" in body.values:
        from paurubot.provisioning.lifecycle import RENEW_MODES

        if body.values["renew_mode"] not in ("", None, *RENEW_MODES):
            raise HTTPException(status_code=422, detail="renew_mode_invalid")
    if "card_pick" in body.values:
        from paurubot.core.cards import CARD_PICKS

        if body.values["card_pick"] not in CARD_PICKS:
            raise HTTPException(status_code=422, detail="card_pick_invalid")
    _check_crypto(body.values)
    _check_services(body.values)
    _check_gateways(body.values)
    _check_business(body.values)
    if "app_links" in body.values:
        from paurubot.provisioning.subscription import validate_app_links

        try:
            body.values["app_links"] = validate_app_links(body.values["app_links"])
        except ValueError:
            raise HTTPException(status_code=422, detail="app_links_invalid") from None
    _check_custom(body.values)
    _check_appearance(body.values)
    _check_growth(body.values)
    await _check_account_extras(db, ctx.shop_id, body.values)
    from paurubot.core import referral_rewards

    try:
        await referral_rewards.check_settings(db, ctx.shop_id, body.values)
    except referral_rewards.RewardError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from None
    before = shop_settings.public_view(await shop_settings.load(db, ctx.shop_id, fresh=True))
    try:
        await shop_settings.set_many(db, ctx.shop_id, body.values)
    except shop_settings.SettingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from paurubot.bot import button_looks, premium_emoji

    premium_emoji.forget(ctx.shop_id)
    button_looks.forget(ctx.shop_id)
    after = shop_settings.public_view(await shop_settings.load(db, ctx.shop_id, fresh=True))
    changed = {k: after[k] for k in body.values}
    if {"loyalty_enabled", "loyalty_tiers"} & set(body.values):
        await queue.enqueue(
            db,
            "ops",
            "loyalty_recount",
            {},
            shop_id=ctx.shop_id,
            dedupe_key=f"loyalty:{ctx.shop_id}:{utcnow():%Y%m%d%H%M%S}",
        )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="settings.update",
        entity="settings",
        before={k: before[k] for k in body.values},
        after=changed,
    )
    return {"values": changed}


# ---------------------------------------------------------------- bot texts


@router.get("/texts")
async def list_texts(ctx: Annotated[StaffContext, Depends(require("texts.write"))], db: DB) -> dict[str, Any]:
    overrides: dict[str, dict[str, str]] = {}
    for row in await db.scalars(select(BotText).where(BotText.shop_id == ctx.shop_id)):
        overrides.setdefault(row.locale, {})[row.key] = row.value
    items = []
    for key in i18n.keys():
        items.append(
            {
                "key": key,
                "variables": sorted(i18n.fields(i18n.catalog(i18n.DEFAULT_LOCALE).get(key, ""))),
                "defaults": {loc: i18n.catalog(loc).get(key, "") for loc in i18n.LOCALES},
                "overrides": {
                    loc: overrides[loc][key] for loc in i18n.LOCALES if key in overrides.get(loc, {})
                },
            }
        )
    return {"locales": list(i18n.LOCALES), "globals": sorted(i18n.GLOBAL_VARIABLES), "items": items}


class TextBody(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


def _check_text_target(locale: str, key: str) -> None:
    if locale not in i18n.LOCALES or key not in i18n.catalog(i18n.DEFAULT_LOCALE):
        raise HTTPException(status_code=404, detail="not_found")


@router.put("/texts/{locale}/{key}")
async def set_text(
    locale: str,
    key: str,
    body: TextBody,
    ctx: Annotated[StaffContext, Depends(require("texts.write"))],
    db: DB,
) -> dict[str, Any]:
    _check_text_target(locale, key)
    problems = i18n.validate_override(key, body.value)
    if problems:
        raise HTTPException(status_code=422, detail={"problems": problems})
    row = await db.scalar(
        select(BotText).where(BotText.shop_id == ctx.shop_id, BotText.locale == locale, BotText.key == key)
    )
    before = row.value if row else None
    if row is None:
        db.add(BotText(shop_id=ctx.shop_id, locale=locale, key=key, value=body.value))
    else:
        row.value = body.value
    await db.flush()
    i18n.invalidate(ctx.shop_id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="text.update",
        entity="text",
        before={"key": key, "locale": locale, "value": before},
        after={"key": key, "locale": locale, "value": body.value},
    )
    return {"key": key, "locale": locale, "value": body.value}


class ResetTexts(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=3000)


@router.post("/texts/reset")
async def reset_texts(
    body: ResetTexts, ctx: Annotated[StaffContext, Depends(require("texts.write"))], db: DB
) -> dict[str, int]:
    """Put many texts back to the shipped wording at once, in every language (a whole tab)."""
    result = await db.execute(
        delete(BotText).where(BotText.shop_id == ctx.shop_id, BotText.key.in_(sorted(set(body.keys))))
    )
    i18n.invalidate(ctx.shop_id)
    count = int(getattr(result, "rowcount", 0) or 0)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="text.reset_many",
        entity="text",
        after={"count": count},
    )
    return {"reset": count}


@router.delete("/texts/{locale}/{key}")
async def reset_text(
    locale: str, key: str, ctx: Annotated[StaffContext, Depends(require("texts.write"))], db: DB
) -> dict[str, Any]:
    _check_text_target(locale, key)
    await db.execute(
        delete(BotText).where(BotText.shop_id == ctx.shop_id, BotText.locale == locale, BotText.key == key)
    )
    i18n.invalidate(ctx.shop_id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="text.reset",
        entity="text",
        after={"key": key, "locale": locale},
    )
    return {"key": key, "locale": locale, "value": i18n.catalog(locale).get(key, "")}


# ---------------------------------------------------------------- channels and contacts

Settings = Annotated[StaffContext, Depends(require("settings.write"))]
SettingsRead = Annotated[StaffContext, Depends(require("settings.read"))]


class ForcedChannelBody(BaseModel):
    chat_id: str = Field(min_length=2, max_length=255)
    title: str = Field(default="", max_length=96)
    url: str = Field(default="", max_length=255)
    sort: int = 0


class ForcedChannelPatch(BaseModel):
    title: str | None = Field(default=None, max_length=96)
    url: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    sort: int | None = None


class ChannelCheckBody(BaseModel):
    chat_id: str = Field(min_length=2, max_length=255)


class TrustChannelBody(BaseModel):
    chat_id: str = Field(min_length=2, max_length=255)
    kinds: list[str] = Field(default_factory=list)
    is_active: bool = True
    sort: int = 0


class TrustChannelPatch(BaseModel):
    kinds: list[str] | None = None
    is_active: bool | None = None
    sort: int | None = None


class LogChannelBody(BaseModel):
    topic: str
    chat_id: int
    thread_id: int | None = None


class ContactBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=8, max_length=255, pattern=r"^(https://|tg://)")
    sort: int = 0


def _invite_link(given: str, resolved: str) -> str:
    """The link customers press: what the admin typed when it is a real link, else the @username."""
    given = given.strip()
    if given.startswith(("https://t.me/", "tg://")) and len(given) > len("https://t.me/"):
        return given[:255]
    return resolved


def _kinds(raw: list[str]) -> list[str]:
    clean = sorted({kind for kind in raw if kind in channel_rules.TRUST_KINDS})
    if not clean:
        raise HTTPException(status_code=422, detail="kinds_required")
    if len(clean) != len(set(raw)):
        raise HTTPException(status_code=422, detail="unknown_kind")
    return clean


@router.get("/channels")
async def list_channels(ctx: SettingsRead, db: DB) -> dict[str, Any]:
    shop = ctx.shop_id
    forced = list(
        await db.scalars(
            select(ForcedChannel)
            .where(ForcedChannel.shop_id == shop)
            .order_by(ForcedChannel.sort, ForcedChannel.id)
        )
    )
    trust = list(
        await db.scalars(
            select(TrustChannel)
            .where(TrustChannel.shop_id == shop)
            .order_by(TrustChannel.sort, TrustChannel.id)
        )
    )
    logs = await db.scalars(select(LogChannel).where(LogChannel.shop_id == shop).order_by(LogChannel.topic))
    contacts = await db.scalars(
        select(SupportContact)
        .where(SupportContact.shop_id == shop)
        .order_by(SupportContact.sort, SupportContact.id)
    )
    # Channels Telegram could not verify or deliver to during the last fifteen minutes.
    warnings = await kv.marked(
        [f"join:{shop}:{row.chat_id}" for row in forced] + [f"trust:{shop}:{row.chat_id}" for row in trust]
    )
    return {
        "forced": [
            {**plain(x, CHANNEL_FIELDS), "warning": warnings.get(f"join:{shop}:{x.chat_id}")} for x in forced
        ],
        "trust": [
            {**plain(x, TRUST_FIELDS), "warning": warnings.get(f"trust:{shop}:{x.chat_id}")} for x in trust
        ],
        "logs": [plain(x, LOG_FIELDS) for x in logs],
        "contacts": [plain(x, CONTACT_FIELDS) for x in contacts],
        "topics": list(LOG_TOPICS),
        "trust_kinds": list(channel_rules.TRUST_KINDS),
    }


@router.post("/channels/check")
async def check_channel(body: ChannelCheckBody, ctx: Settings) -> dict[str, Any]:
    """Resolve a channel with the shop's bot before it is added, so mistakes surface right away."""
    info = await resolve_channel(ctx.shop_id, body.chat_id)
    return {
        "chat_id": info.chat_id,
        "title": info.title,
        "url": info.url,
        "bot_admin": info.bot_admin,
    }


@router.post("/channels/forced")
async def add_forced(body: ForcedChannelBody, ctx: Settings, db: DB) -> dict[str, Any]:
    info = await require_channel(ctx.shop_id, body.chat_id)
    chat_id = str(info.chat_id)
    exists = await db.scalar(
        select(ForcedChannel.id).where(ForcedChannel.shop_id == ctx.shop_id, ForcedChannel.chat_id == chat_id)
    )
    if exists:
        raise HTTPException(status_code=409, detail="channel_exists")
    link = _invite_link(body.url, info.url)
    if not link:
        raise HTTPException(status_code=422, detail="link_required")
    row = ForcedChannel(
        shop_id=ctx.shop_id,
        chat_id=chat_id,
        title=body.title.strip() or info.title or chat_id,
        url=link,
        sort=body.sort,
    )
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.forced_add",
        entity="channel",
        entity_id=row.id,
        after={"chat_id": chat_id, "title": row.title, "url": link, "sort": body.sort},
    )
    return plain(row, CHANNEL_FIELDS)


@router.put("/channels/forced/{row_id}")
async def set_forced(body: ForcedChannelPatch, row_id: int, ctx: Settings, db: DB) -> dict[str, Any]:
    row = await owned(db, ForcedChannel, row_id, ctx)
    fields = body.model_dump(exclude_none=True)
    before = {key: getattr(row, key) for key in fields}
    for key, value in fields.items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.forced_set",
        entity="channel",
        entity_id=row_id,
        before=before,
        after={key: getattr(row, key) for key in fields},
    )
    return plain(row, CHANNEL_FIELDS)


@router.delete("/channels/forced/{row_id}")
async def remove_forced(row_id: int, ctx: Settings, db: DB) -> dict[str, bool]:
    row = await owned(db, ForcedChannel, row_id, ctx)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.forced_remove",
        entity="channel",
        entity_id=row_id,
    )
    return {"ok": True}


@router.post("/channels/trust")
async def add_trust(body: TrustChannelBody, ctx: Settings, db: DB) -> dict[str, Any]:
    kinds = _kinds(body.kinds)
    info = await require_channel(ctx.shop_id, body.chat_id)
    exists = await db.scalar(
        select(TrustChannel.id).where(
            TrustChannel.shop_id == ctx.shop_id, TrustChannel.chat_id == info.chat_id
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="channel_exists")
    row = TrustChannel(
        shop_id=ctx.shop_id,
        chat_id=info.chat_id,
        title=info.title,
        url=info.url,
        kinds=kinds,
        is_active=body.is_active,
        sort=body.sort,
    )
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.trust_add",
        entity="channel",
        entity_id=row.id,
        after={"chat_id": row.chat_id, "kinds": kinds, "is_active": body.is_active},
    )
    return plain(row, TRUST_FIELDS)


@router.put("/channels/trust/{row_id}")
async def set_trust(body: TrustChannelPatch, row_id: int, ctx: Settings, db: DB) -> dict[str, Any]:
    row = await owned(db, TrustChannel, row_id, ctx)
    fields = body.model_dump(exclude_none=True)
    if "kinds" in fields:
        fields["kinds"] = _kinds(fields["kinds"])
    before = {key: getattr(row, key) for key in fields}
    for key, value in fields.items():
        setattr(row, key, value)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.trust_set",
        entity="channel",
        entity_id=row_id,
        before=before,
        after={key: getattr(row, key) for key in fields},
    )
    return plain(row, TRUST_FIELDS)


@router.delete("/channels/trust/{row_id}")
async def remove_trust(row_id: int, ctx: Settings, db: DB) -> dict[str, bool]:
    row = await owned(db, TrustChannel, row_id, ctx)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.trust_remove",
        entity="channel",
        entity_id=row_id,
    )
    return {"ok": True}


@router.put("/channels/logs")
async def set_log_channel(body: LogChannelBody, ctx: Settings, db: DB) -> dict[str, Any]:
    if body.topic not in LOG_TOPICS:
        raise HTTPException(status_code=422, detail="unknown_topic")
    row = await db.scalar(
        select(LogChannel).where(LogChannel.shop_id == ctx.shop_id, LogChannel.topic == body.topic)
    )
    if row is None:
        row = LogChannel(
            shop_id=ctx.shop_id, topic=body.topic, chat_id=body.chat_id, thread_id=body.thread_id
        )
        db.add(row)
    else:
        row.chat_id, row.thread_id = body.chat_id, body.thread_id
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.log_set",
        entity="channel",
        entity_id=row.id,
        after=body.model_dump(),
    )
    return plain(row, LOG_FIELDS)


@router.delete("/channels/logs/{row_id}")
async def remove_log_channel(row_id: int, ctx: Settings, db: DB) -> dict[str, bool]:
    row = await owned(db, LogChannel, row_id, ctx)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.log_remove",
        entity="channel",
        entity_id=row_id,
    )
    return {"ok": True}


@router.post("/contacts")
async def add_contact(body: ContactBody, ctx: Settings, db: DB) -> dict[str, Any]:
    row = SupportContact(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="contact.add",
        entity="contact",
        entity_id=row.id,
        after=body.model_dump(),
    )
    return plain(row, CONTACT_FIELDS)


@router.delete("/contacts/{row_id}")
async def remove_contact(row_id: int, ctx: Settings, db: DB) -> dict[str, bool]:
    row = await owned(db, SupportContact, row_id, ctx)
    await db.delete(row)
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="contact.remove", entity="contact", entity_id=row_id
    )
    return {"ok": True}


# ---------------------------------------------------------------- audit and jobs


@router.get("/audit")
async def audit_log(
    ctx: Annotated[StaffContext, Depends(require("audit.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    action: str | None = Query(None, max_length=64),
    entity: str | None = Query(None, max_length=32),
    entity_id: int | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    stmt = select(AuditLog).where(AuditLog.shop_id == ctx.shop_id)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_type == "staff", AuditLog.actor_id == actor_id)
    rows, total = await paginate(db, stmt.order_by(AuditLog.id.desc()), page)
    return {"items": [plain(r, AUDIT_FIELDS) for r in rows], "total": total}


@router.get("/jobs")
async def list_jobs(
    ctx: Annotated[StaffContext, Depends(require("settings.write"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    status: JobStatus | None = None,
) -> dict[str, Any]:
    stmt = select(Job).where(Job.shop_id == ctx.shop_id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    rows, total = await paginate(db, stmt.order_by(Job.id.desc()), page)
    counts = dict(
        (
            await db.execute(
                select(Job.status, func.count(Job.id)).where(Job.shop_id == ctx.shop_id).group_by(Job.status)
            )
        ).all()
    )
    return {
        "items": [plain(r, JOB_FIELDS) for r in rows],
        "total": total,
        "counts": {s.value: int(counts.get(s, 0)) for s in JobStatus},
    }


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: int, ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, bool]:
    await owned(db, Job, job_id, ctx)
    if not await queue.retry_dead(db, job_id):
        raise HTTPException(status_code=409, detail="not_retryable")
    await record(db, shop_id=ctx.shop_id, actor=ctx.actor, action="job.retry", entity="job", entity_id=job_id)
    return {"ok": True}


class RetryDeadBody(BaseModel):
    queue: str | None = Field(default=None, max_length=32)


@router.post("/jobs/retry-dead")
async def retry_dead_jobs(
    body: RetryDeadBody, ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, int]:
    """Put every dead job of this shop (optionally one queue) back in line (JOB-05)."""
    stmt = select(Job.id).where(Job.shop_id == ctx.shop_id, Job.status == JobStatus.DEAD).limit(5000)
    if body.queue:
        stmt = stmt.where(Job.queue == body.queue)
    ids = list(await db.scalars(stmt))
    retried = 0
    for job_id in ids:
        retried += int(await queue.retry_dead(db, job_id))
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="job.retry_dead",
        entity="job",
        entity_id=None,
        after={"queue": body.queue, "count": retried},
    )
    return {"retried": retried}


@router.get("/doctor")
async def doctor(ctx: Annotated[StaffContext, Depends(require("settings.write"))]) -> dict[str, Any]:
    """Environment analyser (ADM-08)."""
    from paurubot.ops import doctor as doctor_checks

    checks = await doctor_checks.run()
    return {"checks": [c.as_dict() for c in checks], "checked_at": utcnow().isoformat()}


# ---------------------------------------------------------------- reports (FIN-04)

SOLD = (OrderStatus.COMPLETED,)


def _range(start: date | None, end: date | None) -> tuple[datetime, datetime, date, date]:
    today = utcnow().date()
    end = end or today
    start = start or end - timedelta(days=29)
    if start > end:
        raise HTTPException(status_code=422, detail="bad_range")
    if (end - start).days > 366:
        raise HTTPException(status_code=422, detail="range_too_long")
    lo = datetime.combine(start, time.min).replace(tzinfo=utcnow().tzinfo)
    hi = datetime.combine(end + timedelta(days=1), time.min).replace(tzinfo=utcnow().tzinfo)
    return lo, hi, start, end


async def _sales(db: DB, ctx: StaffContext, lo: datetime, hi: datetime) -> dict[str, Any]:
    scope = await reseller_scope(db, ctx)
    from paurubot.core.reports import real_buyer

    base = [
        Order.shop_id == ctx.shop_id,
        Order.status.in_(SOLD),
        Order.completed_at >= lo,
        Order.completed_at < hi,
        real_buyer(),
    ]
    if scope is not None:
        base.append(Order.customer_id.in_(scope))
    day = cast(Order.completed_at, Date)
    by_day = (
        await db.execute(
            select(day, func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
            .where(*base)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    by_plan = (
        await db.execute(
            select(Plan.title, Order.kind, func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
            .select_from(Order)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .where(*base)
            .group_by(Plan.title, Order.kind)
            .order_by(func.sum(Order.amount).desc())
        )
    ).all()
    result: dict[str, Any] = {
        "days": [{"day": d.isoformat(), "orders": int(n), "amount": int(a)} for d, n, a in by_day],
        "plans": [{"plan": t, "kind": k.value, "orders": int(n), "amount": int(a)} for t, k, n, a in by_plan],
        "totals": {
            "orders": sum(int(n) for _, n, _ in by_day),
            "amount": sum(int(a) for _, _, a in by_day),
        },
    }
    if scope is None:
        by_provider = (
            await db.execute(
                select(Payment.provider, func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0))
                .where(
                    Payment.shop_id == ctx.shop_id,
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= lo,
                    Payment.paid_at < hi,
                )
                .group_by(Payment.provider)
                .order_by(Payment.provider)
            )
        ).all()
        result["providers"] = [
            {"provider": p, "payments": int(n), "amount": int(a)} for p, n, a in by_provider
        ]
    return result


@router.get("/reports/sales")
async def sales_report(
    ctx: Annotated[StaffContext, Depends(require("reports.read"))],
    db: DB,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    lo, hi, first, last = _range(start, end)
    data = await _sales(db, ctx, lo, hi)
    return {"start": first.isoformat(), "end": last.isoformat(), **data}


@router.get("/reports/funnel")
async def funnel_report(
    ctx: Annotated[StaffContext, Depends(require("reports.read"))],
    db: DB,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Of the people who joined in the period, how many shared a phone, took a trial and bought (ADM-06)."""
    from paurubot.core.reports import funnel

    lo, hi, first, last = _range(start, end)
    scope = await reseller_scope(db, ctx)
    return {
        "start": first.isoformat(),
        "end": last.isoformat(),
        **await funnel(db, ctx.shop_id, lo, hi, scope),
    }


@router.get("/reports/top-customers")
async def top_customers_report(
    ctx: Annotated[StaffContext, Depends(require("reports.read"))],
    db: DB,
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    from paurubot.core.exports import top_customers

    lo, hi, first, last = _range(start, end)
    scope = await reseller_scope(db, ctx)
    items = await top_customers(db, ctx.shop_id, start=lo, end=hi, limit=limit, customer_ids=scope)
    return {"start": first.isoformat(), "end": last.isoformat(), "items": items}


EXPORT_PERMISSIONS = {
    "orders": "orders.read",
    "payments": "payments.read",
    "customers": "customers.read",
    "services": "services.read",
}


@router.get("/export/{kind}.xlsx")
async def export_xlsx(
    kind: Literal["orders", "payments", "customers", "services"],
    db: DB,
    ctx: Annotated[StaffContext, Depends(require("dashboard.view"))],
    start: date | None = None,
    end: date | None = None,
) -> Response:
    """Excel download of one table, for a date range (FIN-05)."""
    from paurubot.core.exports import build

    if not ctx.can(EXPORT_PERMISSIONS[kind]):
        raise HTTPException(status_code=403, detail="forbidden")
    scope = await reseller_scope(db, ctx)
    if scope is not None and kind == "customers":
        raise HTTPException(status_code=403, detail="forbidden")
    lo = datetime.combine(start, time.min).replace(tzinfo=utcnow().tzinfo) if start else None
    hi = datetime.combine(end + timedelta(days=1), time.min).replace(tzinfo=utcnow().tzinfo) if end else None
    content = await build(db, kind, ctx.shop_id, start=lo, end=hi, customer_ids=scope)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="export.download",
        entity="export",
        entity_id=None,
        after={"kind": kind, "start": str(start or ""), "end": str(end or "")},
    )
    stamp = (end or utcnow().date()).isoformat()
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{kind}-{stamp}.xlsx"'},
    )


@router.get("/reports/sales.csv")
async def sales_csv(
    ctx: Annotated[StaffContext, Depends(require("reports.read"))],
    db: DB,
    start: date | None = None,
    end: date | None = None,
) -> Response:
    lo, hi, first, last = _range(start, end)
    data = await _sales(db, ctx, lo, hi)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["day", "orders", "amount"])
    for row in data["days"]:
        writer.writerow([row["day"], row["orders"], row["amount"]])
    writer.writerow([])
    writer.writerow(["plan", "kind", "orders", "amount"])
    for row in data["plans"]:
        writer.writerow([_csv_safe(row["plan"] or "-"), row["kind"], row["orders"], row["amount"]])
    if "providers" in data:
        writer.writerow([])
        writer.writerow(["provider", "payments", "amount"])
        for row in data["providers"]:
            writer.writerow([row["provider"], row["payments"], row["amount"]])
    name = f"sales-{first.isoformat()}-{last.isoformat()}.csv"
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _csv_safe(value: str) -> str:
    """Neutralise spreadsheet formulas in admin-supplied text (CSV injection)."""
    return "'" + value if value[:1] in {"=", "+", "-", "@", "\t", "\r"} else value


# ---------------------------------------------------------------- backups (OPS-05, OPS-06)

Backups = Annotated[StaffContext, Depends(require("backups.manage"))]


async def _backup_schedule(db: AsyncSession) -> dict[str, Any]:
    from paurubot.config import get_settings
    from paurubot.ops import backup_schedule

    settings = get_settings()
    return {
        **(await backup_schedule.load(db)).as_dict(),
        "passphrase_set": bool(settings.backup_passphrase.get_secret_value()),
        "timezone": settings.timezone,
    }


@router.get("/backups")
async def list_backups(ctx: Backups, db: DB) -> dict[str, Any]:
    from paurubot.ops import backup

    return {
        "items": [
            {"name": b.name, "size": b.size, "created_at": b.created_at.isoformat()} for b in backup.listing()
        ],
        "schedule": await _backup_schedule(db),
    }


class BackupScheduleBody(BaseModel):
    enabled: bool
    every_hours: int = Field(ge=1, le=168)
    hour: int = Field(ge=0, le=23)
    keep: int = Field(ge=1, le=365)
    telegram: bool


@router.put("/backups/schedule")
async def save_backup_schedule(body: BackupScheduleBody, ctx: Backups, db: DB) -> dict[str, Any]:
    from paurubot.core.shops import require_root_shop
    from paurubot.ops import backup_schedule

    if body.every_hours > 24 and body.every_hours % 24:
        # Past a day the backup runs at the set hour, so the interval is whole days.
        raise HTTPException(status_code=422, detail="backup_every_whole_days")
    shop = await require_root_shop(db)
    if ctx.shop_id != shop.id:
        raise HTTPException(status_code=403, detail="backup_root_only")
    before = (await backup_schedule.load(db)).as_dict()
    values = {
        "backup_auto_enabled": body.enabled,
        "backup_every_hours": body.every_hours,
        "backup_hour": body.hour,
        "backup_keep": body.keep,
        "backup_send_telegram": body.telegram,
    }
    await shop_settings.set_many(db, shop.id, values)
    after = (await backup_schedule.load(db)).as_dict()
    await record(
        db,
        shop_id=shop.id,
        actor=ctx.actor,
        action="backup.schedule",
        entity="backup",
        before=before,
        after=after,
    )
    await db.commit()
    return {"schedule": await _backup_schedule(db)}


@router.post("/backups")
async def start_backup(ctx: Backups, db: DB) -> dict[str, Any]:
    from paurubot.config import get_settings

    if not get_settings().backup_passphrase.get_secret_value():
        raise HTTPException(status_code=409, detail="backup_passphrase_missing")
    job_id = await queue.enqueue(
        db,
        "ops",
        "backup",
        {"label": f"staff:{ctx.staff.id}"},
        shop_id=ctx.shop_id,
        dedupe_key=f"backup:manual:{utcnow():%Y%m%d%H%M}",
        max_attempts=2,
    )
    await record(db, shop_id=ctx.shop_id, actor=ctx.actor, action="backup.requested", entity="backup")
    return {"queued": job_id is not None}


@router.post("/backups/{name}/verify")
async def verify_backup(name: str, ctx: Backups, db: DB) -> dict[str, Any]:
    """Check that an archive opens and its database dump is whole (ClockBot's «verify last»)."""
    from paurubot.config import get_settings
    from paurubot.ops import backup

    try:
        path = backup.resolve(name)
        result = await backup.verify(
            path, passphrase=get_settings().backup_passphrase.get_secret_value() or None
        )
    except (backup.BackupError, OSError, RuntimeError, ValueError) as exc:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="backup.verify_failed",
            entity="backup",
            after={"name": name},
        )
        raise HTTPException(status_code=422, detail="backup_verify_failed") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="backup.verified",
        entity="backup",
        after={"name": name},
    )
    return {
        "ok": True,
        "objects": int(result.get("objects") or 0),
        "master_key_included": bool(result.get("master_key_included")),
    }


class BackupBotBody(BaseModel):
    bot_token: str | None = Field(default=None, max_length=128)  # None keeps the saved one; "" clears it
    chat_ids: list[int] = Field(default_factory=list, max_length=10)


@router.get("/backups/telegram")
async def backup_bot_state(ctx: Backups, db: DB) -> dict[str, Any]:
    """Whether a separate backup bot is set (never its token) and its chats."""
    from paurubot.core.shops import require_root_shop

    shop = await require_root_shop(db)
    if ctx.shop_id != shop.id:
        raise HTTPException(status_code=403, detail="backup_root_only")
    cfg = await shop_settings.load(db, shop.id, fresh=True)
    return {"bot": bool(cfg.get("backup_bot_token")), "chat_ids": list(cfg.get("backup_chat_ids") or [])}


@router.put("/backups/telegram")
async def backup_bot(body: BackupBotBody, ctx: Backups, db: DB) -> dict[str, Any]:
    """Send backups with a separate bot to chosen chats (ClockBot parity)."""
    import re

    from paurubot.core.shops import require_root_shop

    shop = await require_root_shop(db)
    if ctx.shop_id != shop.id:
        raise HTTPException(status_code=403, detail="backup_root_only")
    values: dict[str, Any] = {"backup_chat_ids": body.chat_ids}
    if body.bot_token is not None:
        token = body.bot_token.strip()
        if token and not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,64}", token):
            raise HTTPException(status_code=422, detail="bot_token_invalid")
        values["backup_bot_token"] = token
    await shop_settings.set_many(db, shop.id, values)
    await record(
        db,
        shop_id=shop.id,
        actor=ctx.actor,
        action="backup.telegram",
        entity="backup",
        after={"chat_ids": body.chat_ids},
    )
    await db.commit()
    cfg = await shop_settings.load(db, shop.id, fresh=True)
    return {"bot": bool(cfg.get("backup_bot_token")), "chat_ids": list(cfg.get("backup_chat_ids") or [])}


@router.get("/backups/{name}")
async def download_backup(name: str, ctx: Backups, db: DB) -> Response:
    from fastapi.responses import FileResponse

    from paurubot.ops import backup

    try:
        path = backup.resolve(name)
    except backup.BackupError as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="backup.downloaded",
        entity="backup",
        after={"name": name},
    )
    await db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# ---------------------------------------------------------------- updates from the panel (OPS-07)


@router.get("/updates")
async def update_status(ctx: Backups) -> dict[str, Any]:
    from paurubot.ops import updater

    return (await updater.status()).as_dict()


class UpdateBody(BaseModel):
    action: Literal["update", "rollback"]
    channel: Literal["stable", "beta"] | None = None
    version: str | None = Field(default=None, max_length=40)


@router.post("/updates")
async def request_update(body: UpdateBody, ctx: Backups, db: DB) -> dict[str, Any]:
    from paurubot.ops import updater

    try:
        sent = await updater.request(body.action, body.channel, body.version or None)
    except updater.UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"system.{body.action}_requested",
        entity="system",
        after=sent,
    )
    return {"requested": sent}


@router.delete("/updates")
async def cancel_update(ctx: Backups, db: DB) -> dict[str, bool]:
    from paurubot.ops import updater

    canceled = await updater.cancel()
    if canceled:
        await record(
            db, shop_id=ctx.shop_id, actor=ctx.actor, action="system.update_canceled", entity="system"
        )
    return {"canceled": canceled}


# ---------------------------------------------------------------- shop bundle (FIN-07)

MAX_BUNDLE_BYTES = 5 * 1024 * 1024


@router.get("/bundle")
async def export_bundle(ctx: Backups, db: DB) -> Response:
    """The shop's setup (no secrets, no customers) as a JSON file."""
    import json

    from paurubot.core import bundle

    data = await bundle.export(db, ctx.shop_id)
    await record(db, shop_id=ctx.shop_id, actor=ctx.actor, action="bundle.export", entity="shop")
    await db.commit()
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(
        json.dumps(data, ensure_ascii=False, indent=1, default=str),
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="paurubot-bundle-{stamp}.json"'},
    )


@router.post("/bundle")
async def import_bundle(
    ctx: Backups, db: DB, dry_run: bool = True, file: UploadFile = File(...)
) -> dict[str, Any]:
    import json

    from paurubot.core import bundle

    raw = await file.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise HTTPException(status_code=413, detail="bundle_too_large")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bundle_invalid") from exc
    nested = await db.begin_nested()
    try:
        report = await bundle.apply(db, ctx.shop_id, data, dry_run=dry_run)
    except (bundle.BundleError, KeyError, TypeError, ValueError) as exc:
        await nested.rollback()
        detail = str(exc) if isinstance(exc, bundle.BundleError) else "bundle_invalid"
        raise HTTPException(status_code=422, detail=detail) from exc
    if dry_run:
        await nested.rollback()
        shop_settings.invalidate(ctx.shop_id)
    else:
        await nested.commit()
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="bundle.import",
            entity="shop",
            after={"created": report.created, "updated": report.updated},
        )
    return report.as_dict()


# ---------------------------------------------------------------- moving from another bot (MIG-01)

MAX_IMPORT_BYTES = 300 * 1024 * 1024
Owner = Annotated[StaffContext, Depends(require("backups.manage"))]


@router.post("/migration/upload")
async def migration_upload(ctx: Owner, file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a MySQL dump or SQLite file and describe what it holds."""
    import secrets as _secrets

    from paurubot.migration import runner

    token = _secrets.token_hex(12)
    path = runner.upload_path(token)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    size = 0
    with path.open("wb") as handle:  # noqa: ASYNC230  chunks are small and the disk is local
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_IMPORT_BYTES:
                handle.close()
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="import_too_large")
            handle.write(chunk)
    try:
        extract = await runner.load(token, None)
    except (ValueError, UnicodeDecodeError) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="unknown_source") from exc
    return {"token": token, "size": size, **runner.summary(extract)}


class MigrationRunBody(BaseModel):
    token: str = Field(pattern=r"^[a-f0-9]{24}$")
    source: Literal["mirza", "faoxima", "wizwiz"] | None = None
    panel_map: dict[str, int] = Field(default_factory=dict)
    dry_run: bool = True


@router.post("/migration/run")
async def migration_run(body: MigrationRunBody, ctx: Owner, db: DB) -> dict[str, Any]:
    from paurubot.migration import runner

    if not await asyncio.to_thread(runner.upload_path(body.token).is_file):
        raise HTTPException(status_code=404, detail="upload_missing")
    for panel_id in set(body.panel_map.values()):
        await owned(db, Panel, panel_id, ctx)
    job_id = await queue.enqueue(
        db,
        "ops",
        "migrate",
        {
            "token": body.token,
            "source": body.source,
            "panel_map": body.panel_map,
            "dry_run": body.dry_run,
            "shop_id": ctx.shop_id,
            "staff_id": ctx.staff.id,
        },
        shop_id=ctx.shop_id,
        max_attempts=2,
    )
    return {"job_id": job_id}


@router.get("/migration/last")
async def migration_last(ctx: Owner) -> dict[str, Any]:
    import json as _json

    from paurubot import kv

    raw = await kv.get_redis().get(f"migration:{ctx.shop_id}:last")
    return {"report": _json.loads(raw) if raw else None}


# ---------------------------------------------------------------- settings export / import (ClockBot parity)

EXPORT_FORMAT = "paurubot-settings"
# Server-wide, per-bot or credential settings never travel in a file.
_NOT_EXPORTED_GROUPS = {"backups", "own", "resale"}


def _portable(key: str) -> bool:
    spec = shop_settings.SPECS.get(key)
    return (
        spec is not None
        and not spec.secret
        and spec.group not in _NOT_EXPORTED_GROUPS
        and not key.startswith("tonpay_custom_")
        and key != "trial_generation"
    )


@router.get("/settings/export")
async def export_settings(
    ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, Any]:
    """The shop's settings and edited bot texts as one file, without any secret."""
    cfg = await shop_settings.load(db, ctx.shop_id, fresh=True)
    texts = await db.scalars(
        select(BotText).where(BotText.shop_id == ctx.shop_id).order_by(BotText.key, BotText.locale)
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="settings.export",
        entity="shop",
        entity_id=ctx.shop_id,
    )
    return {
        "format": EXPORT_FORMAT,
        "version": 1,
        "settings": {key: cfg[key] for key in sorted(cfg) if _portable(key)},
        "texts": [{"locale": row.locale, "key": row.key, "value": row.value} for row in texts],
    }


class ImportedText(BaseModel):
    locale: str = Field(max_length=8)
    key: str = Field(max_length=64)
    value: str = Field(max_length=4096)


class SettingsFile(BaseModel):
    format: str
    version: int = 1
    settings: dict[str, Any] = Field(default_factory=dict)
    texts: list[ImportedText] = Field(default_factory=list, max_length=5000)


@router.post("/settings/import")
async def import_settings(
    body: SettingsFile, ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, int]:
    """Apply a settings file: the same checks as saving each setting; secrets and unknown keys are
    skipped, and texts are imported only with the right to edit them."""
    if body.format != EXPORT_FORMAT:
        raise HTTPException(status_code=422, detail="settings_file_invalid")
    if body.texts and not ctx.can("texts.write"):  # checked first: nothing is written half way
        raise HTTPException(status_code=403, detail="forbidden")
    values = {key: value for key, value in body.settings.items() if _portable(key)}
    skipped = len(body.settings) - len(values)
    if values:
        await write_settings(SettingsBody(values=values), ctx, db)
    imported_texts = 0
    if body.texts:
        for item in body.texts:
            if item.locale not in i18n.LOCALES or item.key not in i18n.catalog(i18n.DEFAULT_LOCALE):
                skipped += 1
                continue
            if i18n.validate_override(item.key, item.value):
                skipped += 1
                continue
            row = await db.scalar(
                select(BotText).where(
                    BotText.shop_id == ctx.shop_id, BotText.locale == item.locale, BotText.key == item.key
                )
            )
            if row is None:
                db.add(BotText(shop_id=ctx.shop_id, locale=item.locale, key=item.key, value=item.value))
            else:
                row.value = item.value
            imported_texts += 1
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="settings.import",
        entity="shop",
        entity_id=ctx.shop_id,
        after={"settings": len(values), "texts": imported_texts, "skipped": skipped},
    )
    await db.commit()
    if imported_texts:
        i18n.invalidate(ctx.shop_id)
    return {"settings": len(values), "texts": imported_texts, "skipped": skipped}
