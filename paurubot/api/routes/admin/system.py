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

from paurubot import i18n
from paurubot.api.admin_common import Page, owned, page_params, paginate, plain, reseller_scope
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
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
)
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])

LOG_TOPICS = (
    "all",
    "orders",
    "payments",
    "users",
    "errors",
    "services",
    "reports",
    "backups",
    "resellers",
    "panels",
)
CHANNEL_FIELDS = ["id", "chat_id", "title", "url", "sort"]
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
    if values.get("fx_source") not in (None, "nobitex", "manual"):
        raise HTTPException(status_code=422, detail="fx_source_invalid")


@router.put("/settings")
async def write_settings(
    body: SettingsBody, ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, Any]:
    unknown = [k for k in body.values if k not in shop_settings.SPECS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown_settings:{','.join(sorted(unknown))}")
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
    _check_crypto(body.values)
    if "app_links" in body.values:
        from paurubot.provisioning.subscription import validate_app_links

        try:
            body.values["app_links"] = validate_app_links(body.values["app_links"])
        except ValueError:
            raise HTTPException(status_code=422, detail="app_links_invalid") from None
    _check_custom(body.values)
    _check_appearance(body.values)
    _check_growth(body.values)
    before = shop_settings.public_view(await shop_settings.load(db, ctx.shop_id, fresh=True))
    try:
        await shop_settings.set_many(db, ctx.shop_id, body.values)
    except shop_settings.SettingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    chat_id: str = Field(min_length=2, max_length=64, pattern=r"^(@[A-Za-z0-9_]{4,}|-100\d{5,})$")
    title: str = Field(min_length=1, max_length=96)
    url: str = Field(min_length=8, max_length=255, pattern=r"^https://t\.me/")
    sort: int = 0


class LogChannelBody(BaseModel):
    topic: str
    chat_id: int
    thread_id: int | None = None


class ContactBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=8, max_length=255, pattern=r"^(https://|tg://)")
    sort: int = 0


@router.get("/channels")
async def list_channels(ctx: SettingsRead, db: DB) -> dict[str, Any]:
    shop = ctx.shop_id
    forced = await db.scalars(
        select(ForcedChannel)
        .where(ForcedChannel.shop_id == shop)
        .order_by(ForcedChannel.sort, ForcedChannel.id)
    )
    logs = await db.scalars(select(LogChannel).where(LogChannel.shop_id == shop).order_by(LogChannel.topic))
    contacts = await db.scalars(
        select(SupportContact)
        .where(SupportContact.shop_id == shop)
        .order_by(SupportContact.sort, SupportContact.id)
    )
    return {
        "forced": [plain(x, CHANNEL_FIELDS) for x in forced],
        "logs": [plain(x, LOG_FIELDS) for x in logs],
        "contacts": [plain(x, CONTACT_FIELDS) for x in contacts],
        "topics": list(LOG_TOPICS),
    }


@router.post("/channels/forced")
async def add_forced(body: ForcedChannelBody, ctx: Settings, db: DB) -> dict[str, Any]:
    exists = await db.scalar(
        select(ForcedChannel.id).where(
            ForcedChannel.shop_id == ctx.shop_id, ForcedChannel.chat_id == body.chat_id
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="channel_exists")
    row = ForcedChannel(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="channel.forced_add",
        entity="channel",
        entity_id=row.id,
        after=body.model_dump(),
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
    base = [
        Order.shop_id == ctx.shop_id,
        Order.status.in_(SOLD),
        Order.completed_at >= lo,
        Order.completed_at < hi,
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


@router.get("/backups")
async def list_backups(ctx: Backups) -> dict[str, Any]:
    from paurubot.config import get_settings
    from paurubot.ops import backup

    settings = get_settings()
    return {
        "items": [
            {"name": b.name, "size": b.size, "created_at": b.created_at.isoformat()} for b in backup.listing()
        ],
        "schedule": {
            "hour": settings.backup_hour,
            "keep": settings.backup_keep,
            "telegram": settings.backup_to_telegram,
            "passphrase_set": bool(settings.backup_passphrase.get_secret_value()),
            "timezone": settings.timezone,
        },
    }


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
