"""Permission-aware admin reply keyboard and categorized shortcuts.

Only navigation lives here: existing callback handlers still enforce permissions,
tenant isolation and confirmation for their own actions. No synthetic updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from paurubot.bot.callbacks import Admin, Rb
from paurubot.bot.context import StaffIdentity
from paurubot.bot.utils import Btn, StaffOnly, show
from paurubot.config import get_settings
from paurubot.db.models import StaffRole
from paurubot.i18n import Texts

router = Router(name="admin_navigation")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())


@dataclass(frozen=True)
class Entry:
    key: str
    section: str
    permission: str | None = None
    action: str = ""
    page: int = 0
    id: int = 0

    def target(self) -> Admin:
        return Admin(section=self.section, action=self.action, page=self.page, id=self.id)


# Telegram places the first item on the left; pairs match the Persian reference.
HOME_ROWS = (
    ("stats",),
    ("resellers",),
    ("panels", "addpanel"),
    ("custom",),
    ("users", "shop"),
    ("finance",),
    ("support", "guide"),
    ("updates", "features"),
    ("settings", "receipts"),
)
GROUPS: dict[str, tuple[Entry, ...]] = {
    "stats": (
        Entry("adm_stats", "stats"),
        Entry("adm_hub_stats", "pstats", "reports.read"),
        Entry("adm_finance", "finance", "reports.read"),
    ),
    "resellers": (
        Entry("adm_applications", "applications", "resellers.manage"),
        Entry("adm_ulist_resellers", "ulist", "customers.read", action="f", id=5),
    ),
    "panels": (
        Entry("adm_panels", "panels", "panels.read"),
        Entry("adm_pcfg_btn", "pcfg", "panels.write"),
    ),
    "addpanel": (),  # Secure web form, never ask for panel credentials in this menu.
    "custom": (Entry("adm_x_group_custom", "xcfg", "settings.write", action="grp", page=2),),
    "users": (
        Entry("adm_users_balance", "ulist", "customers.read", action="uf", id=1),
        Entry("adm_users_referrals", "ulist", "customers.read", action="uf", id=3),
        Entry("adm_users_cards", "ulist", "customers.read", action="uf", id=7),
        Entry("adm_users_negative", "ulist", "customers.read", action="uf", id=2),
        Entry("adm_users_active", "ulist", "customers.read", action="uf", id=4),
        Entry("adm_users_resellers", "ulist", "customers.read", action="uf", id=5),
        Entry("adm_users_all", "ulist", "customers.read", action="uf", id=0),
        Entry("adm_users_order", "uorders", "orders.read", action="show"),
        Entry("adm_users_charge", "gift_all", "wallet.adjust"),
        Entry("adm_users_lookup", "lookup", "customers.read"),
        Entry("adm_users_message", "broadcast", "broadcast.send"),
        Entry("adm_users_bulk", "ubulk", "services.write", action="show"),
    ),
    "shop": (
        Entry("adm_plans", "plans", "catalog.write", action="show"),
        Entry("adm_s_categories", "scats", "catalog.write", action="show"),
        Entry("adm_s_discount_manage", "dc", "catalog.write", action="show"),
        Entry("adm_s_gift_manage", "gc", "wallet.adjust", action="show"),
        Entry("adm_s_bulk", "sconf", "settings.write", action="show"),
    ),
    "finance": (
        Entry("adm_finance", "finance", "reports.read", action="show"),
        Entry("adm_payments", "payments", "payments.review", action="show"),
        Entry("adm_f_gateways", "fgw", "settings.write", action="show"),
        Entry("adm_f_card", "fcard", "settings.write", action="show"),
        Entry("adm_f_bonus", "fbonus", "settings.write", action="show"),
        Entry("adm_gc_btn", "gc", "wallet.adjust", action="show"),
    ),
    "support": (
        Entry("adm_tickets", "tickets", "tickets.reply"),
        Entry("adm_broadcast", "broadcast", "broadcast.send"),
        Entry("adm_bc_unpin", "bc_unpin", "broadcast.send"),
    ),
    "guide": (),
    "updates": (Entry("adm_backups", "backups", "backups.manage"),),
    "features": (
        Entry("adm_feat_group_main", "feat", "settings.write", action="cat", page=0),
        Entry("adm_feat_group_users", "feat", "settings.write", action="cat", page=1),
        Entry("adm_feat_group_sales", "feat", "settings.write", action="cat", page=2),
        Entry("adm_feat_group_growth", "feat", "settings.write", action="cat", page=3),
        Entry("adm_feat_group_payments", "fgw", "settings.write", action="show"),
        Entry("adm_cfg_group_crons", "cfg", "settings.write", action="grp", page=1),
        Entry("adm_look", "look", "settings.write"),
        Entry("adm_status", "status", "settings.write"),
        Entry("adm_x_btn", "xcfg", "settings.write"),
        Entry("adm_grw_btn", "grw", "settings.write"),
    ),
    "receipts": (Entry("adm_payments", "payments", "payments.review"),),
    # Fixed links of the general settings hub; its switches carry live values (admin_general).
    "settings": (
        Entry("adm_gs_main", "feat", "settings.write", action="cat", page=0),
        Entry("adm_hub_features", "feat", "settings.write"),
        Entry("adm_gs_look", "look", "settings.write"),
        Entry("adm_cfg_group_crons", "cfg", "settings.write", action="grp", page=1),
        Entry("adm_gs_values", "settings", "settings.write", action="show"),
        Entry("adm_status", "status", "settings.write"),
        Entry("adm_backups", "backups", "backups.manage"),
    ),
}
LABEL_KEYS = tuple(f"adm_nav_{group}" for group in GROUPS) + ("adm_nav_back",)


def entries(group: str, staff: StaffIdentity) -> list[Entry]:
    items = [item for item in GROUPS.get(group, ()) if item.permission is None or staff.can(item.permission)]
    if group == "users":
        items = [
            item
            for item in items
            if staff.can("customers.read") and (item.section != "ubulk" or staff.can("wallet.adjust"))
        ]
    return items


def customer_bot(staff: StaffIdentity, cfg: dict[str, Any] | None = None) -> bool:
    """A customer's own bot (a reseller's bot or an own-panel bot): it is run from the bot and its
    Mini App, so nothing in it points to the web panel."""
    return staff.role == StaffRole.MERCHANT or bool(cfg and cfg.get("own_bot"))


def mini_app_page(cfg: dict[str, Any] | None) -> str:
    """Where a customer bot's manager lands in the Mini App: the own-panel shop, else the dashboard."""
    return "shop" if cfg and cfg.get("own_bot") else "manage"


def allowed(group: str, staff: StaffIdentity, cfg: dict[str, Any] | None = None) -> bool:
    if group == "resellers" and cfg and cfg.get("own_bot"):
        return False  # a customer's dedicated bot only sells: no resellers to run there (OPB)
    if group in {"addpanel", "updates"} and customer_bot(staff, cfg):
        return False  # the main shop's panels and updates; nothing for a customer bot's manager
    if group == "guide":
        return True
    if group == "addpanel":
        return staff.can("panels.write") and staff.can("panels.read")
    if group == "updates":
        return staff.can("backups.manage")
    if group == "resellers" and staff.role == StaffRole.MERCHANT:
        return True
    return bool(entries(group, staff))


def menu(t: Texts, cfg: dict[str, Any], staff: StaffIdentity) -> ReplyKeyboardMarkup:
    """Same calling convention as start.menu, for admin-only completion messages."""
    rows: list[list[KeyboardButton]] = []
    for group_row in HOME_ROWS:
        row = [KeyboardButton(text=t(f"adm_nav_{g}")) for g in group_row if allowed(g, staff, cfg)]
        if row:
            rows.append(row)
    rows.append([KeyboardButton(text=t("adm_nav_back"))])
    # Not persistent: the phone's back button must be able to hide it (the keyboard icon reopens it).
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def home_kb(t: Texts, staff: StaffIdentity, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    """Compact category links for old inline home/maintenance messages."""
    rows = [
        [
            InlineKeyboardButton(text=t(f"adm_nav_{g}"), callback_data=Admin(section="nav", action=g).pack())
            for g in row
            if allowed(g, staff, cfg)
        ]
        for row in HOME_ROWS
    ]
    rows = [row for row in rows if row]
    rows.append(
        [
            InlineKeyboardButton(
                text=t("adm_nav_back"), callback_data=Admin(section="nav", action="exit").pack()
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_kb(t: Texts, staff: StaffIdentity, group: str, cfg: dict[str, Any]) -> InlineKeyboardMarkup:
    if group == "shop":
        from paurubot.bot.routers.admin_shop import home_kb as shop_kb

        return shop_kb(t, staff)
    if group == "finance":
        from paurubot.bot.routers.admin_finance import home_kb as finance_kb

        return finance_kb(t, staff)
    if group == "users":
        from paurubot.bot.routers.admin_users import home_kb as user_kb

        return user_kb(t, staff)
    if group == "settings":
        from paurubot.bot.routers.admin_general import hub_kb

        return hub_kb(t, staff, cfg)
    if group == "features":
        from paurubot.bot.routers.admin_hub import features_kb

        return features_kb(t, cfg)
    kb = InlineKeyboardBuilder()
    for item in entries(group, staff):
        label = t(item.key)
        if item.section == "maintenance":
            label = t("adm_maintenance_on" if cfg.get("bot_enabled", True) else "adm_maintenance_off")
        kb.button(text=label, callback_data=item.target())
    if group == "resellers" and staff.role == StaffRole.MERCHANT:
        kb.button(text=t("rb_btn_hub"), callback_data=Rb(action="home"))
    kb.adjust(2)
    url = get_settings().public_url.rstrip("/")
    if customer_bot(staff, cfg):
        # Run from the bot and its Mini App: the guide opens the Mini App's management page.
        if group == "guide" and url.startswith("https://"):
            app = WebAppInfo(url=f"{url}/app/{mini_app_page(cfg)}")
            kb.row(InlineKeyboardButton(text=t("adm_nav_open_app"), web_app=app))
    else:
        web_pages = {"addpanel": "panels", "updates": "updates", "guide": "help"}
        if url and group in web_pages:
            kb.row(InlineKeyboardButton(text=t("adm_nav_open_web"), url=f"{url}/admin/{web_pages[group]}"))
    kb.row(InlineKeyboardButton(text=t("adm_nav_home"), callback_data=Admin(section="home").pack()))
    return kb.as_markup()


def group_text(t: Texts, group: str, *, customer_bot: bool = False, own_bot: bool = False) -> str:
    if group == "shop":
        return t("adm_s_home")
    if group == "finance":
        return t("adm_f_home")
    if group == "users":
        return t("adm_users_home")
    if group == "settings":
        return t("adm_gs_home")
    if group == "features":
        return t("adm_feat_title")
    if group == "guide" and customer_bot:
        return t("adm_nav_guide_own" if own_bot else "adm_nav_guide_resale")
    if group in {"guide", "addpanel", "updates"}:
        suffix = "text" if get_settings().public_url else "no_web"
        return t(f"adm_nav_{group}_{suffix}")
    return t("adm_nav_group_text", title=t(f"adm_nav_{group}"))


@router.message(Btn(*LABEL_KEYS))
async def navigate(
    message: Message, t: Texts, staff: StaffIdentity, cfg: dict[str, Any], state: FSMContext
) -> None:
    if t.matches(message.text, "adm_nav_back"):
        await state.clear()
        from paurubot.bot.routers.start import home

        await home(message, t, cfg, staff)
        return
    group = next((g for g in GROUPS if t.matches(message.text, f"adm_nav_{g}")), "")
    if not allowed(group, staff, cfg):
        await message.answer(t("adm_forbidden"))
        return
    await state.clear()
    # Restore the persistent admin keyboard if a canceled input had replaced it.
    await message.answer(t("adm_nav_open", title=t(f"adm_nav_{group}")), reply_markup=menu(t, cfg, staff))
    text = group_text(t, group, customer_bot=customer_bot(staff, cfg), own_bot=bool(cfg.get("own_bot")))
    await message.answer(text, reply_markup=group_kb(t, staff, group, cfg))


@router.callback_query(Admin.filter(F.section == "nav"))
async def navigate_cb(
    call: CallbackQuery,
    callback_data: Admin,
    t: Texts,
    staff: StaffIdentity,
    cfg: dict[str, Any],
    state: FSMContext,
) -> None:
    group = callback_data.action
    if group == "exit":
        await state.clear()
        await call.answer()
        if isinstance(call.message, Message):
            from paurubot.bot.routers.start import home

            await home(call.message, t, cfg, staff)
        return
    if not allowed(group, staff, cfg):
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    text = group_text(t, group, customer_bot=customer_bot(staff, cfg), own_bot=bool(cfg.get("own_bot")))
    await show(call, text, group_kb(t, staff, group, cfg))
