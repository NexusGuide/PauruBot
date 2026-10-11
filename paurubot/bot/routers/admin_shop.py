"""Shop navigation, product editing and category management with reviewed changes."""

from __future__ import annotations

import re
import secrets
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.routers.admin_finance import digest, locked_setting
from paurubot.bot.utils import StaffOnly, show
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc, money, parse_int
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Category,
    Location,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    PanelKind,
    Plan,
    PlanAudience,
)
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.provisioning.lifecycle import RENEW_MODES

router = Router(name="admin_shop")
router.callback_query.filter(StaffOnly())
router.message.filter(StaffOnly())
PAGE = 10
TTL = 900
FIELD_BOUNDS = {
    "price": (0, 10**11),
    "days": (0, 3650),
    "data_gb": (0, 100000),
    "ip_limit": (0, 100),
    "sort": (-1000000, 1000000),
}
TEXT_FIELDS = {"title": 96, "description": 1000, "username_prefix": 16}
BOOLEAN_FIELDS = ("is_active", "renewable", "on_hold")
FIELDS = (*FIELD_BOUNDS, *TEXT_FIELDS, *BOOLEAN_FIELDS)
SNAPSHOT = (
    *FIELDS,
    "audience",
    "renew_mode",
    "category_id",
    "panel_id",
    "location_id",
    "panel_options",
    "addons",
    "delivery",
)
TERMS = {"days", "data_gb", "ip_limit", "on_hold", "renew_mode", "panel_id", "location_id", "username_prefix"}
HOLD_KINDS = {
    PanelKind.MARZBAN,
    PanelKind.PASARGUARD,
    PanelKind.XUI,
    PanelKind.MARZNESHIN,
    PanelKind.HIDDIFY,
    PanelKind.GUARD,
}
STOCK_FIELDS = {"title", "description", "price", "sort", "is_active", "category_id", "audience"}
BULK_KEYS = ("bulk_buy_enabled", "bulk_buy_max", "bulk_buy_resellers_only")


class ShopAdminStates(StatesGroup):
    value = State()
    bulk_value = State()


def button(
    t: Texts, label: str, section: str, id: int = 0, action: str = "show", page: int = 0
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=t(label), callback_data=Admin(section=section, id=id, action=action, page=page).pack()
    )


def home_kb(t: Texts, staff: StaffIdentity) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if staff.can("catalog.write"):
        kb.row(button(t, "adm_plans", "plans"), button(t, "adm_s_categories", "scats"))
    if staff.can("wallet.adjust"):
        kb.row(button(t, "adm_s_gift_add", "gc", action="add"), button(t, "adm_s_gift_manage", "gc"))
    if staff.can("catalog.write"):
        kb.row(button(t, "adm_s_discount_add", "dc", action="add"), button(t, "adm_s_discount_manage", "dc"))
    if staff.can("settings.write"):
        kb.row(button(t, "adm_s_bulk", "sconf"))
    if staff.can("catalog.write") and get_settings().public_url:
        kb.row(
            InlineKeyboardButton(
                text=t("adm_s_web_catalog"), url=get_settings().public_url.rstrip("/") + "/admin/catalog"
            )
        )
    kb.row(button(t, "adm_ud_admin", "home"))
    return kb.as_markup()


async def owned(
    session: AsyncSession, shop_id: int, kind: str, id: int, *, lock: bool = False
) -> Plan | Category | None:
    if kind == "plan":
        stmt = select(Plan).where(Plan.id == id, Plan.shop_id == shop_id)
    else:
        stmt = select(Category).where(Category.id == id, Category.shop_id == shop_id)  # type: ignore[assignment]
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(stmt)


def snapshot(row: Plan | Category) -> dict[str, Any]:
    keys = SNAPSHOT if isinstance(row, Plan) else ("title", "sort", "is_active")
    return {
        key: getattr(row, key).value if hasattr(getattr(row, key), "value") else getattr(row, key)
        for key in keys
    }


async def holds_supported(session: AsyncSession, plan: Plan, changes: dict[str, Any] | None = None) -> bool:
    values = {**snapshot(plan), **(changes or {})}
    if values["delivery"] != "panel" or values["days"] <= 0:
        return False
    if values["panel_id"]:
        panel = await session.get(Panel, values["panel_id"])
        return panel is not None and panel.shop_id == plan.shop_id and panel.kind in HOLD_KINDS
    rows = list(
        await session.scalars(
            select(Panel).where(
                Panel.shop_id == plan.shop_id,
                Panel.location_id == values["location_id"],
                Panel.is_active.is_(True),
            )
        )
    )
    return bool(rows) and all(panel.kind in HOLD_KINDS for panel in rows)


async def open_terms_orders(session: AsyncSession, plan: Plan) -> bool:
    return bool(
        await session.scalar(
            select(Order.id)
            .where(
                Order.shop_id == plan.shop_id,
                Order.plan_id == plan.id,
                or_(
                    Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
                    (Order.status == OrderStatus.PENDING)
                    & or_(Order.expires_at.is_(None), Order.expires_at > utcnow()),
                ),
            )
            .limit(1)
        )
    )


async def changes_valid(
    session: AsyncSession, shop: ShopRow, row: Plan | Category, changes: dict[str, Any]
) -> bool:
    for key, value in changes.items():
        if key in FIELD_BOUNDS:
            low, high = FIELD_BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                return False
        elif key in BOOLEAN_FIELDS and type(value) is not bool:
            return False
        elif key in TEXT_FIELDS:
            if value is None and key in {"description", "username_prefix"}:
                continue
            if not isinstance(value, str) or not value or len(value) > TEXT_FIELDS[key]:
                return False
            if any(ord(c) < 32 and (key != "description" or c not in "\n\t") for c in value):
                return False
            if key == "username_prefix" and not re.fullmatch(r"[a-z0-9_]{1,16}", value):
                return False
        elif key == "audience" and value not in {p.value for p in PlanAudience}:
            return False
        elif key == "renew_mode" and value not in (None, *RENEW_MODES):
            return False
    if isinstance(row, Category):
        return set(changes) <= {"title", "sort", "is_active"} and (
            "title" not in changes or 1 <= len(changes["title"]) <= 64
        )
    if not set(changes) <= set(SNAPSHOT) - {"panel_options", "addons", "delivery"}:
        return False
    if row.delivery == "stock" and not set(changes) <= STOCK_FIELDS:
        return False
    if any(key in TERMS for key in changes) and await open_terms_orders(session, row):
        return False
    values = {**snapshot(row), **changes}
    if (
        values.get("on_hold")
        and set(changes) & {"on_hold", "days", "panel_id", "location_id"}
        and not await holds_supported(session, row, changes)
    ):
        return False
    for key, model in (("category_id", Category), ("panel_id", Panel), ("location_id", Location)):
        if key in changes and changes[key] is not None:
            target: Any = await session.get(model, changes[key])
            if target is None or target.shop_id != shop.id:
                return False
    if "panel_id" in changes or "location_id" in changes:
        if not values["panel_id"] and not values["location_id"]:
            return False
        if row.panel_options and (values["panel_id"], values["location_id"]) != (
            row.panel_id,
            row.location_id,
        ):
            return False  # never reuse or silently erase driver-specific overrides on a new server
    return True


def product_kb(t: Texts, plan: Plan, *, hold: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    def field(key: str) -> InlineKeyboardButton:
        return button(t, f"adm_s_field_{key}", "pedit", plan.id, "ask", FIELDS.index(key))

    kb.row(field("price"), field("data_gb"), field("days")) if plan.delivery != "stock" else kb.row(
        field("price")
    )
    kb.row(field("title"), button(t, "adm_s_audience", "pedit", plan.id, "audience"))
    if plan.delivery != "stock":
        kb.row(field("description"), button(t, "adm_s_renew_mode", "pedit", plan.id, "renew"))
        kb.row(
            button(t, "adm_s_category", "pedit", plan.id, "category"),
            button(t, "adm_s_destination", "pedit", plan.id, "destination"),
        )
        kb.row(field("ip_limit"), field("username_prefix"))
    else:
        kb.row(field("description"), button(t, "adm_s_category", "pedit", plan.id, "category"))
    toggles = []
    for key in BOOLEAN_FIELDS:
        if plan.delivery == "stock" and key != "is_active":
            continue
        if key == "on_hold" and not (hold or plan.on_hold):
            continue
        toggles.append(
            InlineKeyboardButton(
                text=("✅ " if getattr(plan, key) else "❌ ") + t(f"adm_s_field_{key}"),
                callback_data=Admin(
                    section="pedit",
                    action="off" if getattr(plan, key) else "on",
                    id=plan.id,
                    page=FIELDS.index(key),
                ).pack(),
            )
        )
    for offset in range(0, len(toggles), 2):
        kb.row(*toggles[offset : offset + 2])
    kb.row(field("sort"))
    public = get_settings().public_url.rstrip("/")
    if public:
        kb.row(InlineKeyboardButton(text=t("adm_s_web_technical"), url=f"{public}/admin/catalog"))
    kb.row(button(t, "adm_s_back_products", "plans"), button(t, "adm_ud_admin", "home"))
    kb.row(button(t, "adm_ud_close", "pedit", plan.id, "close"))
    return kb.as_markup()


async def product_text(session: AsyncSession, t: Texts, plan: Plan) -> str:
    async def label(model: Any, id: int | None) -> str:
        row = await session.get(model, id) if id else None
        if row is None or row.shop_id != plan.shop_id:
            return "—"
        return esc(row.name if isinstance(row, Panel) else row.title)

    dest = await label(Panel, plan.panel_id) if plan.panel_id else await label(Location, plan.location_id)
    sold = int(
        await session.scalar(
            select(func.count(Order.id)).where(
                Order.shop_id == plan.shop_id,
                Order.plan_id == plan.id,
                Order.kind == OrderKind.PURCHASE,
                Order.status == OrderStatus.COMPLETED,
                Order.amount > 0,
            )
        )
        or 0
    )
    return t(
        "adm_s_product_card",
        title=esc(plan.title),
        id=plan.id,
        price=money(plan.price),
        gb=plan.data_gb or t("unlimited"),
        days=plan.days or t("unlimited"),
        audience=t(f"adm_s_audience_{plan.audience.value}"),
        category=await label(Category, plan.category_id),
        destination=dest,
        renew=t(f"adm_s_mode_{plan.renew_mode or 'inherit'}"),
        description=esc((plan.description or "—")[:260]),
        sold=sold,
        delivery=esc(plan.delivery),
        state=t("adm_on" if plan.is_active else "adm_off"),
        sort=plan.sort,
        ip=plan.ip_limit or t("unlimited"),
        prefix=esc(plan.username_prefix or "—"),
    )


async def show_product(event: Message | CallbackQuery, session: AsyncSession, t: Texts, plan: Plan) -> None:
    await show(
        event,
        await product_text(session, t, plan),
        product_kb(t, plan, hold=await holds_supported(session, plan)),
    )


async def preview(
    event: Message | CallbackQuery,
    kind: str,
    row: Plan | Category | None,
    changes: dict[str, Any],
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    nonce = secrets.randbelow(2**31 - 1) + 1
    await state.set_state(None)
    await state.set_data(
        {
            "shop_pending": {
                "kind": kind,
                "id": row.id if row else 0,
                "shop": shop.id,
                "before": digest(snapshot(row)) if row else None,
                "changes": changes,
                "nonce": nonce,
                "at": utcnow().timestamp(),
            }
        }
    )
    lines = [t("adm_s_confirm_head", name=esc(row.title if row else t("adm_s_new_category")))]
    for key, value in changes.items():
        old = getattr(row, key) if row else "—"

        def shown(value: Any) -> str:
            return (
                t("adm_on" if value else "adm_off")
                if isinstance(value, bool)
                else esc(value.value if hasattr(value, "value") else value if value is not None else "—")
            )

        lines.append(
            t("adm_s_confirm_line", field=t(f"adm_s_field_{key}"), before=shown(old), after=shown(value))
        )
    lines.append(t("adm_s_change_note"))
    section = "pedit" if kind == "plan" else "scats"
    id = row.id if row else 0
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [button(t, "adm_s_apply", section, id, "confirm", nonce)],
            [button(t, "btn_cancel", section, id, "show" if kind == "plan" or row is None else "view")],
        ]
    )
    await show(event, "\n\n".join(lines), kb)


async def confirm(
    call: CallbackQuery,
    data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
    kind: str,
) -> None:
    pending = (await state.get_data()).get("shop_pending")
    if (
        not isinstance(pending, dict)
        or pending.get("kind") != kind
        or pending.get("shop") != shop.id
        or pending.get("id") != data.id
        or pending.get("nonce") != data.page
        or utcnow().timestamp() - float(pending.get("at") or 0) > TTL
    ):
        await call.answer(t("button_expired"), show_alert=True)
        return
    changes = pending["changes"]
    if data.id == 0 and kind == "category":
        await session.execute(select(ShopRow.id).where(ShopRow.id == shop.id).with_for_update())
        if await session.scalar(
            select(Category.id)
            .where(Category.shop_id == shop.id, Category.title == changes["title"])
            .limit(1)
        ):
            await state.clear()
            await call.answer(t("adm_s_category_exists"), show_alert=True)
            return
        if (
            set(changes) != {"title"}
            or not isinstance(changes["title"], str)
            or not 1 <= len(changes["title"]) <= 64
        ):
            await state.clear()
            await call.answer(t("adm_s_invalid_input"), show_alert=True)
            return
        row: Plan | Category = Category(shop_id=shop.id, **changes)
        session.add(row)
        await session.flush()
        before = None
    else:
        found = await owned(session, shop.id, kind, data.id, lock=True)
        if found is None or digest(snapshot(found)) != pending["before"]:
            await state.clear()
            await call.answer(t("adm_s_changed"), show_alert=True)
            return
        row = found
        if not await changes_valid(session, shop, row, changes):
            await state.clear()
            await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
            return
        before = {
            key: getattr(row, key).value if hasattr(getattr(row, key), "value") else getattr(row, key)
            for key in changes
        }
        for key, value in changes.items():
            setattr(row, key, PlanAudience(value) if key == "audience" else value)
    await state.clear()
    if before != changes:
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="plan.bot_update" if kind == "plan" else "category.bot_update",
            entity=kind,
            entity_id=row.id,
            before=before,
            after=changes,
        )
    await session.commit()
    await call.answer(t("adm_saved"))
    if isinstance(row, Plan):
        await show_product(call, session, t, row)
    else:
        await show_category(call, session, shop, t, row)


@router.callback_query(Admin.filter(F.section == "pedit"), StaffOnly("catalog.write"))
async def product(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    changes: dict[str, Any]
    action = callback_data.action
    if action == "confirm":
        await confirm(call, callback_data, session, shop, t, staff, state, "plan")
        return
    found = await owned(session, shop.id, "plan", callback_data.id)
    if not isinstance(found, Plan):
        await call.answer(t("plan_unavailable"), show_alert=True)
        return
    plan = found
    if action == "close":
        await state.clear()
        await call.answer()
        await show(call, t("adm_s_closed"))
        return
    if action in {"ask", "on", "off"}:
        if not 0 <= callback_data.page < len(FIELDS):
            await call.answer(t("button_expired"), show_alert=True)
            return
        field = FIELDS[callback_data.page]
        if plan.delivery == "stock" and field not in STOCK_FIELDS:
            await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
            return
        if action == "ask" and field in (*FIELD_BOUNDS, *TEXT_FIELDS):
            await state.clear()
            await state.set_state(ShopAdminStates.value)
            await state.update_data(
                shop_kind="plan", shop_id=shop.id, row_id=plan.id, field=field, before=digest(snapshot(plan))
            )
            await call.answer()
            limits = (
                f"{FIELD_BOUNDS[field][0]}–{FIELD_BOUNDS[field][1]}"
                if field in FIELD_BOUNDS
                else str(TEXT_FIELDS[field])
            )
            await show(
                call,
                t("adm_s_ask", name=t(f"adm_s_field_{field}"), limits=limits),
                InlineKeyboardMarkup(inline_keyboard=[[button(t, "btn_cancel", "pedit", plan.id)]]),
            )
            return
        if action in {"on", "off"} and field in BOOLEAN_FIELDS:
            changes = {field: action == "on"}
            if not await changes_valid(session, shop, plan, changes):
                await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
                return
            await call.answer()
            await preview(call, "plan", plan, changes, shop, t, state)
            return
    if action in {"audience", "renew"}:
        if plan.delivery == "stock" and action == "renew":
            await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        choices = tuple(p.value for p in PlanAudience) if action == "audience" else (None, *RENEW_MODES)
        for index, value in enumerate(choices):
            kb.row(
                button(
                    t,
                    f"adm_s_audience_{value}" if action == "audience" else f"adm_s_mode_{value or 'inherit'}",
                    "pedit",
                    plan.id,
                    "set_aud" if action == "audience" else "set_renew",
                    index,
                )
            )
        kb.row(button(t, "btn_back", "pedit", plan.id))
        await call.answer()
        await show(call, t("adm_s_choose"), kb.as_markup())
        return
    if action in {"category", "destination", "pool"}:
        if plan.delivery == "stock" and action != "category":
            await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
            return
        await picker(call, callback_data, session, shop, t, plan)
        return
    changes = {}
    if action == "set_aud" and 0 <= callback_data.page < len(PlanAudience):
        changes = {"audience": tuple(p.value for p in PlanAudience)[callback_data.page]}
    elif action == "set_renew" and 0 <= callback_data.page <= len(RENEW_MODES):
        changes = {"renew_mode": (None, *RENEW_MODES)[callback_data.page]}
    elif action == "set_cat":
        changes = {"category_id": callback_data.page or None}
    elif action == "set_panel":
        changes = {"panel_id": callback_data.page, "location_id": None}
    elif action == "set_pool":
        changes = {"location_id": callback_data.page, "panel_id": None}
    if changes:
        if not await changes_valid(session, shop, plan, changes):
            await call.answer(t("adm_s_invalid_refs_or_busy"), show_alert=True)
            return
        await call.answer()
        await preview(call, "plan", plan, changes, shop, t, state)
        return
    if action != "show":
        await call.answer(t("button_expired"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await show_product(call, session, t, plan)


async def picker(
    call: CallbackQuery, data: Admin, session: AsyncSession, shop: ShopRow, t: Texts, plan: Plan
) -> None:
    model: Any = Category if data.action == "category" else Location if data.action == "pool" else Panel
    total = int(await session.scalar(select(func.count(model.id)).where(model.shop_id == shop.id)) or 0)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(0, data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(model).where(model.shop_id == shop.id).order_by(model.id).offset(page * PAGE).limit(PAGE)
        )
    )
    kb = InlineKeyboardBuilder()
    setter = "set_cat" if data.action == "category" else "set_pool" if data.action == "pool" else "set_panel"
    if data.action == "category":
        kb.row(button(t, "adm_s_no_category", "pedit", plan.id, "set_cat", 0))
    if data.action == "destination":
        kb.row(button(t, "adm_s_pool", "pedit", plan.id, "pool"))
    for row in rows:
        label = ("✅ " if row.is_active else "⚪️ ") + (row.name if isinstance(row, Panel) else row.title)
        kb.row(
            InlineKeyboardButton(
                text=label[:64],
                callback_data=Admin(section="pedit", action=setter, id=plan.id, page=row.id).pack(),
            )
        )
    nav = []
    if page > 0:
        nav.append(button(t, "adm_ud_previous", "pedit", plan.id, data.action, page - 1))
    if page + 1 < pages:
        nav.append(button(t, "adm_ud_next", "pedit", plan.id, data.action, page + 1))
    if nav:
        kb.row(*nav)
    kb.row(button(t, "btn_back", "pedit", plan.id))
    await call.answer()
    await show(call, t("adm_s_picker", page=page + 1, pages=pages), kb.as_markup())


async def show_category(
    event: Message | CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, row: Category
) -> None:
    plans = int(
        await session.scalar(
            select(func.count(Plan.id)).where(Plan.shop_id == shop.id, Plan.category_id == row.id)
        )
        or 0
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        button(t, "adm_s_field_title", "scats", row.id, "rename"),
        button(t, "adm_s_field_sort", "scats", row.id, "sort"),
    )
    kb.row(
        button(
            t,
            "adm_s_category_off" if row.is_active else "adm_s_category_on",
            "scats",
            row.id,
            "off" if row.is_active else "on",
        )
    )
    kb.row(button(t, "btn_back", "scats"), button(t, "adm_ud_admin", "home"))
    await show(
        event,
        t(
            "adm_s_category_card",
            name=esc(row.title),
            plans=plans,
            sort=row.sort,
            state=t("adm_on" if row.is_active else "adm_off"),
        ),
        kb.as_markup(),
    )


@router.callback_query(Admin.filter(F.section == "scats"), StaffOnly("catalog.write"))
async def categories(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action == "confirm":
        await confirm(call, callback_data, session, shop, t, staff, state, "category")
        return
    if action == "add":
        row = None
    elif action in {"view", "rename", "sort", "on", "off"}:
        found = await owned(session, shop.id, "category", callback_data.id)
        if not isinstance(found, Category):
            await call.answer(t("button_expired"), show_alert=True)
            return
        row = found
    else:
        if action not in {"show", ""}:
            await call.answer(t("button_expired"), show_alert=True)
            return
        await state.clear()
        total = int(
            await session.scalar(select(func.count(Category.id)).where(Category.shop_id == shop.id)) or 0
        )
        pages = max(1, (total + PAGE - 1) // PAGE)
        page = min(max(0, callback_data.page), pages - 1)
        rows = list(
            await session.scalars(
                select(Category)
                .where(Category.shop_id == shop.id)
                .order_by(Category.sort, Category.id)
                .offset(page * PAGE)
                .limit(PAGE)
            )
        )
        kb = InlineKeyboardBuilder()
        for cat in rows:
            kb.row(
                InlineKeyboardButton(
                    text=("✅ " if cat.is_active else "⚪️ ") + cat.title[:50],
                    callback_data=Admin(section="scats", action="view", id=cat.id).pack(),
                )
            )
        nav = []
        if page > 0:
            nav.append(button(t, "adm_ud_previous", "scats", page=page - 1))
        if page + 1 < pages:
            nav.append(button(t, "adm_ud_next", "scats", page=page + 1))
        if nav:
            kb.row(*nav)
        kb.row(button(t, "adm_s_category_add", "scats", action="add"))
        kb.row(button(t, "adm_s_back_shop", "nav", action="shop"), button(t, "adm_ud_admin", "home"))
        await call.answer()
        await show(call, t("adm_s_categories_head", total=total, page=page + 1, pages=pages), kb.as_markup())
        return
    if action in {"add", "rename", "sort"}:
        await state.clear()
        await state.set_state(ShopAdminStates.value)
        await state.update_data(
            shop_kind="category",
            shop_id=shop.id,
            row_id=row.id if row else 0,
            field="sort" if action == "sort" else "title",
            before=digest(snapshot(row)) if row else None,
        )
        await call.answer()
        await show(
            call,
            t("adm_s_category_ask_sort" if action == "sort" else "adm_s_category_ask_title"),
            InlineKeyboardMarkup(inline_keyboard=[[button(t, "btn_cancel", "scats")]]),
        )
        return
    await call.answer()
    assert row is not None
    if action in {"on", "off"}:
        await preview(call, "category", row, {"is_active": action == "on"}, shop, t, state)
    else:
        await state.clear()
        await show_category(call, session, shop, t, row)


@router.message(ShopAdminStates.value, F.text)
async def input_value(
    message: Message, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("shop_id") != shop.id or not staff.can("catalog.write"):
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    kind, field = data.get("shop_kind"), str(data.get("field") or "")
    if (
        kind not in {"plan", "category"}
        or (kind == "plan" and field not in (*FIELD_BOUNDS, *TEXT_FIELDS))
        or (kind == "category" and field not in {"title", "sort"})
    ):
        await state.clear()
        return
    row = await owned(session, shop.id, kind, int(data.get("row_id") or 0)) if data.get("row_id") else None
    if data.get("row_id") and (row is None or digest(snapshot(row)) != data.get("before")):
        await state.clear()
        await message.answer(t("adm_s_changed"))
        return
    raw = (message.text or "").strip()
    value: Any
    if field in FIELD_BOUNDS:
        value = parse_int(raw)
        low, high = FIELD_BOUNDS[field]
        valid = value is not None and low <= value <= high
    elif field == "username_prefix":
        value = None if raw == "-" else raw.lower()
        valid = value is None or bool(re.fullmatch(r"[a-z0-9_]{1,16}", value))
    else:
        limit = 64 if kind == "category" else TEXT_FIELDS[field]
        value = None if field == "description" and raw == "-" else raw
        valid = (
            (value is None or len(value) <= limit)
            and (field == "description" or bool(value))
            and not any(ord(c) < 32 and (field != "description" or c not in "\n\t") for c in raw)
        )
    if not valid:
        await message.answer(t("adm_s_invalid_input"))
        return
    changes = {field: value}
    if row is not None and not await changes_valid(session, shop, row, changes):
        await message.answer(t("adm_s_invalid_refs_or_busy"))
        return
    await preview(message, kind, row, changes, shop, t, state)


async def bulk_page(event: Message | CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    kb = InlineKeyboardBuilder()
    for index, key in enumerate(BULK_KEYS):
        if key == "bulk_buy_max":
            kb.row(button(t, "adm_s_bulk_max", "sconf", index, "ask"))
        else:
            kb.row(
                InlineKeyboardButton(
                    text=("✅ " if cfg.get(key) else "❌ ") + t(f"adm_s_{key}"),
                    callback_data=Admin(
                        section="sconf", id=index, action="off" if cfg.get(key) else "on"
                    ).pack(),
                )
            )
    kb.row(button(t, "adm_s_back_shop", "nav", action="shop"), button(t, "adm_ud_admin", "home"))
    await show(
        event,
        t(
            "adm_s_bulk_card",
            limit=cfg["bulk_buy_max"],
            enabled=t("adm_on" if cfg["bulk_buy_enabled"] else "adm_off"),
            resellers=t("adm_on" if cfg["bulk_buy_resellers_only"] else "adm_off"),
        ),
        kb.as_markup(),
    )


async def bulk_preview(
    event: Message | CallbackQuery,
    key: str,
    value: Any,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
    *,
    before: str | None = None,
) -> None:
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    nonce = secrets.randbelow(2**31 - 1) + 1
    await state.set_state(None)
    await state.set_data(
        {
            "bulk_pending": {
                "key": key,
                "value": value,
                "shop": shop.id,
                "nonce": nonce,
                "before": before or digest(cfg[key]),
                "at": utcnow().timestamp(),
            }
        }
    )

    def shown(value: Any) -> str:
        return t("adm_on" if value else "adm_off") if isinstance(value, bool) else str(value)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [button(t, "adm_s_apply", "sconf", BULK_KEYS.index(key), "confirm", nonce)],
            [button(t, "btn_cancel", "sconf")],
        ]
    )
    await show(
        event,
        t("adm_s_confirm_line", field=t(f"adm_s_{key}"), before=shown(cfg[key]), after=shown(value))
        + "\n\n"
        + t("adm_s_change_note"),
        kb,
    )


@router.callback_query(Admin.filter(F.section == "sconf"), StaffOnly("settings.write"))
async def bulk_settings(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
    state: FSMContext,
) -> None:
    action = callback_data.action
    if action in {"show", ""}:
        await state.clear()
        await call.answer()
        await bulk_page(call, session, shop, t)
        return
    if not 0 <= callback_data.id < len(BULK_KEYS):
        await call.answer(t("button_expired"), show_alert=True)
        return
    key = BULK_KEYS[callback_data.id]
    if action == "confirm":
        pending = (await state.get_data()).get("bulk_pending")
        if (
            not isinstance(pending, dict)
            or pending.get("shop") != shop.id
            or pending.get("key") != key
            or pending.get("nonce") != callback_data.page
            or utcnow().timestamp() - float(pending.get("at") or 0) > TTL
        ):
            await call.answer(t("button_expired"), show_alert=True)
            return
        # All bulk controls share one policy snapshot; serialize confirmations on the same rows.
        for lock_key in sorted(BULK_KEYS):
            await locked_setting(session, shop.id, lock_key)
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        value = pending.get("value")
        if digest(cfg[key]) != pending.get("before"):
            await state.clear()
            await call.answer(t("adm_s_changed"), show_alert=True)
            return
        if (key == "bulk_buy_max" and (type(value) is not int or not 2 <= value <= 200)) or (
            key != "bulk_buy_max" and type(value) is not bool
        ):
            await state.clear()
            await call.answer(t("adm_s_invalid_input"), show_alert=True)
            return
        await state.clear()
        if cfg[key] != value:
            await shop_settings.set_many(session, shop.id, {key: value})
            await record(
                session,
                shop_id=shop.id,
                actor=actor(staff),
                action="settings.bulk_update",
                entity="shop",
                entity_id=shop.id,
                before={key: cfg[key]},
                after={key: value},
            )
        await session.commit()
        await call.answer(t("adm_saved"))
        await bulk_page(call, session, shop, t)
        return
    if action == "ask" and key == "bulk_buy_max":
        cfg = await shop_settings.load(session, shop.id, fresh=True)
        await state.clear()
        await state.set_state(ShopAdminStates.bulk_value)
        await state.set_data({"bulk_shop": shop.id, "bulk_before": digest(cfg[key])})
        await call.answer()
        await show(
            call,
            t("adm_s_bulk_ask"),
            InlineKeyboardMarkup(inline_keyboard=[[button(t, "btn_cancel", "sconf")]]),
        )
        return
    if action in {"on", "off"} and key != "bulk_buy_max":
        await call.answer()
        await bulk_preview(call, key, action == "on", session, shop, t, state)
        return
    await call.answer(t("button_expired"), show_alert=True)


@router.message(ShopAdminStates.bulk_value, F.text)
async def bulk_input(
    message: Message, session: AsyncSession, shop: ShopRow, t: Texts, staff: StaffIdentity, state: FSMContext
) -> None:
    data = await state.get_data()
    if not staff.can("settings.write") or data.get("bulk_shop") != shop.id:
        await state.clear()
        await message.answer(t("adm_forbidden"))
        return
    value = parse_int(message.text)
    if value is None or not 2 <= value <= 200:
        await message.answer(t("adm_s_invalid_input"))
        return
    await bulk_preview(
        message, "bulk_buy_max", value, session, shop, t, state, before=data.get("bulk_before")
    )
