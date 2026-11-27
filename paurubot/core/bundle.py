"""Shop bundle: the shop's setup as one JSON file, for copying it to another install (FIN-07).

A bundle carries settings, catalog (locations, categories, plans), discount codes, reseller
groups, bot texts, channels and support contacts. It never carries secrets, customers, wallets,
orders or panel credentials. Rows are matched by their natural key (a title, a code, a chat id),
so importing the same bundle twice changes nothing the second time. Panels are referenced by
name; a plan whose panel does not exist on the target falls back to its location.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import __version__
from paurubot.core import channels as channel_rules
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    AccountPlan,
    BotText,
    Category,
    DiscountCode,
    DiscountKind,
    ForcedChannel,
    Location,
    LogChannel,
    Panel,
    Plan,
    PlanAudience,
    ResellerGroup,
    SupportContact,
    TrustChannel,
)

FORMAT = "paurubot-bundle"
VERSION = 1
PANEL_SETTINGS = ("trial_panel_id", "custom_panel_id")
PANEL_LIST_SETTINGS = ("trial_panel_ids", "service_import_panel_ids")
LOCATION_SETTINGS = ("custom_location_id",)
# Server-wide settings (the backup schedule) belong to the server, not to the shop's setup.
SERVER_GROUPS = ("backups",)
PLAN_KEYS = (
    "title",
    "description",
    "price",
    "days",
    "data_gb",
    "ip_limit",
    "renewable",
    "sort",
    "is_active",
    "button_style",
    "on_hold",
    "panel_options",
    "username_prefix",
    "addons",
    "delivery",
)
CODE_KEYS = (
    "value",
    "max_discount",
    "min_amount",
    "max_uses",
    "per_customer_limit",
    "first_purchase_only",
    "sticky",
    "is_active",
    "note",
)


class BundleError(Exception):
    pass


@dataclass(slots=True)
class Report:
    dry_run: bool
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def count(self, kind: str, new: bool) -> None:
        bucket = self.created if new else self.updated
        bucket[kind] = bucket.get(kind, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def export(session: AsyncSession, shop_id: int) -> dict[str, Any]:
    cfg = await shop_settings.load(session, shop_id, fresh=True)
    panels = {p.id: p.name for p in await session.scalars(select(Panel).where(Panel.shop_id == shop_id))}
    locations = list(await session.scalars(select(Location).where(Location.shop_id == shop_id)))
    location_names = {loc.id: loc.title for loc in locations}
    categories = list(await session.scalars(select(Category).where(Category.shop_id == shop_id)))
    category_names = {c.id: c.title for c in categories}
    plans = list(await session.scalars(select(Plan).where(Plan.shop_id == shop_id).order_by(Plan.id)))
    plan_names = {p.id: p.title for p in plans}
    account_plan_names = {
        p.id: p.title
        for p in await session.scalars(select(AccountPlan).where(AccountPlan.shop_id == shop_id))
    }

    settings: dict[str, Any] = {}
    refs: dict[str, str] = {}
    for key, spec in shop_settings.SPECS.items():
        if spec.secret or spec.group in SERVER_GROUPS:
            continue
        value = cfg.get(key)
        if key in PANEL_SETTINGS:
            if value and int(value) in panels:
                refs[key] = panels[int(value)]
            continue
        if key in PANEL_LIST_SETTINGS:
            names = [panels[int(pid)] for pid in (value or []) if int(pid) in panels]
            if names:
                refs[key] = names  # type: ignore[assignment]
            continue
        if key in LOCATION_SETTINGS:
            if value and int(value) in location_names:
                refs[key] = location_names[int(value)]
            continue
        if value != spec.default:
            settings[key] = value

    return {
        "format": FORMAT,
        "version": VERSION,
        "app_version": __version__,
        "settings": settings,
        "setting_refs": refs,
        "locations": [
            {"title": x.title, "flag": x.flag, "sort": x.sort, "is_active": x.is_active} for x in locations
        ],
        "categories": [
            {
                "title": x.title,
                "sort": x.sort,
                "is_active": x.is_active,
                "parent": category_names.get(x.parent_id) if x.parent_id else None,
            }
            for x in categories
        ],
        "plans": [
            {
                **{k: getattr(p, k) for k in PLAN_KEYS},
                "audience": p.audience.value,
                "location": location_names.get(p.location_id) if p.location_id else None,
                "category": category_names.get(p.category_id) if p.category_id else None,
                "panel": panels.get(p.panel_id) if p.panel_id else None,
            }
            for p in plans
        ],
        "discount_codes": [
            {
                "code": c.code,
                "kind": c.kind.value,
                **{k: getattr(c, k) for k in CODE_KEYS},
                "plans": [plan_names[i] for i in (c.plan_ids or []) if i in plan_names],
                "account_plans": [
                    account_plan_names[i]
                    for i in (c.account_plan_ids or [])
                    if i in account_plan_names
                ],
                "starts_at": _iso(c.starts_at),
                "expires_at": _iso(c.expires_at),
            }
            for c in await session.scalars(select(DiscountCode).where(DiscountCode.shop_id == shop_id))
        ],
        "reseller_groups": [
            {
                "title": g.title,
                "discount_percent": g.discount_percent,
                "min_topup": g.min_topup,
                "max_active_services": g.max_active_services,
                "is_active": g.is_active,
            }
            for g in await session.scalars(select(ResellerGroup).where(ResellerGroup.shop_id == shop_id))
        ],
        "bot_texts": [
            {"locale": b.locale, "key": b.key, "value": b.value}
            for b in await session.scalars(select(BotText).where(BotText.shop_id == shop_id))
        ],
        "forced_channels": [
            {"chat_id": f.chat_id, "title": f.title, "url": f.url, "sort": f.sort}
            for f in await session.scalars(select(ForcedChannel).where(ForcedChannel.shop_id == shop_id))
        ],
        "log_channels": [
            {"topic": lc.topic, "chat_id": lc.chat_id, "thread_id": lc.thread_id}
            for lc in await session.scalars(select(LogChannel).where(LogChannel.shop_id == shop_id))
        ],
        "trust_channels": [
            {
                "chat_id": tc.chat_id,
                "title": tc.title,
                "url": tc.url,
                "kinds": tc.kinds,
                "is_active": tc.is_active,
                "sort": tc.sort,
            }
            for tc in await session.scalars(select(TrustChannel).where(TrustChannel.shop_id == shop_id))
        ],
        "support_contacts": [
            {"title": s.title, "url": s.url, "sort": s.sort}
            for s in await session.scalars(select(SupportContact).where(SupportContact.shop_id == shop_id))
        ],
    }


def _check(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise BundleError("bundle_invalid")
    if int(bundle.get("version") or 0) > VERSION:
        raise BundleError("bundle_too_new")
    for key in (
        "locations",
        "categories",
        "plans",
        "discount_codes",
        "reseller_groups",
        "bot_texts",
        "forced_channels",
        "log_channels",
        "trust_channels",
        "support_contacts",
    ):
        if not isinstance(bundle.get(key, []), list):
            raise BundleError("bundle_invalid")
    return bundle


def _when(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


def _safe_link(value: Any) -> str:
    text = str(value or "").strip()[:255]
    if not text:
        return ""
    if text.startswith(("https://", "tg://")):
        return text
    return ""


async def apply(session: AsyncSession, shop_id: int, raw: Any, *, dry_run: bool = False) -> Report:
    """Import a bundle into this shop. Caller commits (dry runs are rolled back by the caller)."""
    from paurubot import i18n

    bundle = _check(raw)
    report = Report(dry_run)

    async def one(model: Any, **where: Any) -> Any:
        stmt = select(model).where(model.shop_id == shop_id)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return await session.scalar(stmt.limit(1))

    def put(kind: str, row: Any, values: dict[str, Any], model: Any) -> Any:
        if row is None:
            row = model(shop_id=shop_id, **values)
            session.add(row)
            report.count(kind, True)
        else:
            changed = False
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                report.count(kind, False)
        return row

    for item in bundle.get("locations", []):
        row = await one(Location, title=str(item["title"])[:64])
        values = {
            "title": str(item["title"])[:64],
            "flag": item.get("flag"),
            "sort": int(item.get("sort") or 0),
        }
        put("locations", row, {**values, "is_active": bool(item.get("is_active", True))}, Location)
    for item in bundle.get("categories", []):
        row = await one(Category, title=str(item["title"])[:64])
        put(
            "categories",
            row,
            {
                "title": str(item["title"])[:64],
                "sort": int(item.get("sort") or 0),
                "is_active": bool(item.get("is_active", True)),
            },
            Category,
        )
    await session.flush()

    locations = {
        x.title: x.id for x in await session.scalars(select(Location).where(Location.shop_id == shop_id))
    }
    category_rows = {
        x.title: x for x in await session.scalars(select(Category).where(Category.shop_id == shop_id))
    }
    categories = {title: row.id for title, row in category_rows.items()}
    # Subcategories, once every category exists: a parent must be a top-level category here.
    for item in bundle.get("categories", []):
        row = category_rows.get(str(item["title"])[:64])
        parent = category_rows.get(str(item.get("parent") or "")[:64])
        if row is None or not item.get("parent"):
            continue
        if parent is None or parent.parent_id is not None or parent.id == row.id:
            report.warnings.append(f"category {row.title}: parent {item.get('parent')} skipped")
            continue
        row.parent_id = parent.id
    panels = {x.name: x.id for x in await session.scalars(select(Panel).where(Panel.shop_id == shop_id))}

    for item in bundle.get("plans", []):
        panel_id = panels.get(item.get("panel")) if item.get("panel") else None
        location_id = locations.get(item.get("location")) if item.get("location") else None
        if item.get("panel") and panel_id is None:
            report.warnings.append(f"plan {item.get('title')}: panel {item.get('panel')} is not here")
        delivery = str(item.get("delivery") or "panel")
        if delivery != "stock" and panel_id is None and location_id is None:
            report.warnings.append(f"plan {item.get('title')}: skipped, no panel or location here")
            continue
        values = {k: item[k] for k in PLAN_KEYS if k in item}
        values["title"] = str(values.get("title", ""))[:96]
        values.update(
            audience=PlanAudience(item.get("audience") or "all"),
            location_id=location_id,
            category_id=categories.get(item.get("category")) if item.get("category") else None,
            panel_id=panel_id,
            # Panel-specific choices (inbounds, groups) only make sense on the same panel.
            panel_options=values.get("panel_options") if panel_id else {},
        )
        row = await one(Plan, title=values["title"], location_id=location_id)
        put("plans", row, values, Plan)
    await session.flush()
    plan_ids = {x.title: x.id for x in await session.scalars(select(Plan).where(Plan.shop_id == shop_id))}
    account_plan_ids = {
        x.title: x.id
        for x in await session.scalars(select(AccountPlan).where(AccountPlan.shop_id == shop_id))
    }

    for item in bundle.get("discount_codes", []):
        code = str(item["code"])[:32]
        values = {k: item.get(k) for k in CODE_KEYS if k in item}
        values.update(
            code=code,
            kind=DiscountKind(item.get("kind") or "percent"),
            plan_ids=[plan_ids[name] for name in item.get("plans", []) if name in plan_ids],
            account_plan_ids=[
                account_plan_ids[name]
                for name in item.get("account_plans", [])
                if name in account_plan_ids
            ],
            starts_at=_when(item.get("starts_at")),
            expires_at=_when(item.get("expires_at")),
        )
        put("discount_codes", await one(DiscountCode, code=code), values, DiscountCode)
    for item in bundle.get("reseller_groups", []):
        title = str(item["title"])[:64]
        values = {
            "title": title,
            "discount_percent": max(0, min(100, int(item.get("discount_percent") or 0))),
            "min_topup": int(item.get("min_topup") or 0),
            "max_active_services": item.get("max_active_services"),
            "is_active": bool(item.get("is_active", True)),
        }
        put("reseller_groups", await one(ResellerGroup, title=title), values, ResellerGroup)
    for item in bundle.get("bot_texts", []):
        locale, key, value = str(item["locale"]), str(item["key"]), str(item["value"])
        problems = i18n.validate_override(key, value)
        if problems:
            report.warnings.append(f"text {locale}/{key}: {problems[0]}")
            continue
        put(
            "bot_texts",
            await one(BotText, locale=locale, key=key),
            {"locale": locale, "key": key, "value": value},
            BotText,
        )
    for item in bundle.get("forced_channels", []):
        chat = str(item["chat_id"])[:64]
        url = _safe_link(item.get("url"))
        if item.get("url") and not url:
            report.warnings.append(f"forced channel {chat}: unsafe url skipped")
            continue
        values = {
            "chat_id": chat,
            "title": str(item.get("title") or "")[:96],
            "url": url,
        }
        put(
            "forced_channels",
            await one(ForcedChannel, chat_id=chat),
            {**values, "sort": int(item.get("sort") or 0)},
            ForcedChannel,
        )
    for item in bundle.get("log_channels", []):
        topic = str(item["topic"])[:32]
        values = {"topic": topic, "chat_id": int(item["chat_id"]), "thread_id": item.get("thread_id")}
        put("log_channels", await one(LogChannel, topic=topic), values, LogChannel)
    for item in bundle.get("trust_channels", []):
        chat_id = int(item["chat_id"])
        kinds = [kind for kind in (item.get("kinds") or []) if kind in channel_rules.TRUST_KINDS]
        if not kinds:
            report.warnings.append(f"trust channel {chat_id}: no report kinds, skipped")
            continue
        url = _safe_link(item.get("url"))
        if item.get("url") and not url:
            report.warnings.append(f"trust channel {chat_id}: unsafe url skipped")
            continue
        values = {
            "chat_id": chat_id,
            "title": str(item.get("title") or "")[:96],
            "url": url,
            "kinds": kinds,
            "is_active": bool(item.get("is_active", True)),
        }
        put(
            "trust_channels",
            await one(TrustChannel, chat_id=chat_id),
            {**values, "sort": int(item.get("sort") or 0)},
            TrustChannel,
        )
    for item in bundle.get("support_contacts", []):
        title = str(item["title"])[:64]
        url = _safe_link(item.get("url"))
        if not url:
            report.warnings.append(f"support contact {title}: invalid url skipped")
            continue
        values = {"title": title, "url": url, "sort": int(item.get("sort") or 0)}
        put("support_contacts", await one(SupportContact, title=title), values, SupportContact)

    settings: dict[str, Any] = {}
    for key, value in (bundle.get("settings") or {}).items():
        spec = shop_settings.SPECS.get(key)
        if (
            spec is None
            or spec.secret
            or spec.group in SERVER_GROUPS
            or key in PANEL_SETTINGS
            or key in PANEL_LIST_SETTINGS
            or key in LOCATION_SETTINGS
        ):
            report.warnings.append(f"setting {key}: ignored")
            continue
        settings[key] = value
    for key, name in (bundle.get("setting_refs") or {}).items():
        if key in PANEL_LIST_SETTINGS and isinstance(name, list):
            resolved = [panels[item] for item in name if isinstance(item, str) and item in panels]
            if resolved:
                settings[key] = resolved
            else:
                report.warnings.append(f"setting {key}: no panels are here")
            continue
        target = (
            panels.get(name)
            if key in PANEL_SETTINGS
            else locations.get(name)
            if key in LOCATION_SETTINGS
            else None
        )
        if target is None:
            report.warnings.append(f"setting {key}: {name} is not here")
            continue
        settings[key] = target
    try:
        await shop_settings.set_many(session, shop_id, settings)
    except shop_settings.SettingError as exc:
        raise BundleError(str(exc)) from exc
    if settings:
        report.updated["settings"] = len(settings)
    await session.flush()
    return report
