"""Individual customer navigation and shop-scoped, read-only payment history."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot.callbacks import Admin
from paurubot.bot.routers.admin_hub import USER_FILTERS, user_lists
from paurubot.bot.utils import StaffOnly, clear_customer_input, show
from paurubot.core import customers
from paurubot.core.formatting import esc, money
from paurubot.db.models import Payment
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts

router = Router(name="admin_customer")
router.callback_query.filter(StaffOnly())
PAGE = 10


@router.callback_query(
    Admin.filter(F.section.in_({"uprofile", "uprofile_close"})), StaffOnly("customers.read")
)
async def navigation(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    state: FSMContext,
) -> None:
    if callback_data.section == "uprofile_close" or callback_data.action == "close":
        await state.clear()
        await call.answer()
        await show(call, t("adm_ud_closed"))
        return
    customer = await customers.get(session, shop.id, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    data = await state.get_data()
    origin = data.get("user_list_origin")
    target = Admin(section="ulist", action="uf")
    if (
        isinstance(origin, dict)
        and origin.get("shop_id") == shop.id
        and origin.get("customer_id") == customer.id
    ):
        index, page, action = origin.get("filter"), origin.get("page"), origin.get("action")
        if (
            isinstance(index, int)
            and 0 <= index < len(USER_FILTERS)
            and isinstance(page, int)
            and action in {"uf", "f"}
        ):
            target = Admin(section="ulist", action=action, id=index, page=max(0, page))
    await clear_customer_input(state)
    await user_lists(call, target, session, shop, t)


@router.callback_query(
    Admin.filter(F.section == "upay"), StaffOnly("customers.read"), StaffOnly("payments.read")
)
async def payment_history(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
) -> None:
    customer = await customers.get(session, shop.id, callback_data.id)
    if customer is None:
        await call.answer(t("adm_customer_not_found"), show_alert=True)
        return
    conds = [Payment.shop_id == shop.id, Payment.customer_id == customer.id]
    total = int(await session.scalar(select(func.count(Payment.id)).where(*conds)) or 0)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = min(max(0, callback_data.page), pages - 1)
    rows = list(
        await session.scalars(
            select(Payment).where(*conds).order_by(Payment.id.desc()).offset(page * PAGE).limit(PAGE)
        )
    )
    lines = [
        t("adm_ud_payments_head", name=esc(customer.display_name), total=total, page=page + 1, pages=pages)
    ]
    for row in rows:
        lines.append(
            t(
                "adm_ud_payment_line",
                id=row.id,
                amount=money(row.amount),
                currency=esc(row.currency),
                provider=esc(row.provider),
                status=esc(t(f"adm_ud_payment_{row.status.value}")),
                date=row.created_at.strftime("%Y-%m-%d %H:%M UTC"),
            )
        )
    if not rows:
        lines.append(t("adm_ud_payment_empty"))
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_previous"),
                callback_data=Admin(section="upay", id=customer.id, page=page - 1).pack(),
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text=t("adm_ud_next"),
                callback_data=Admin(section="upay", id=customer.id, page=page + 1).pack(),
            )
        )
    if nav:
        kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text=t("btn_back"), callback_data=Admin(section="user", id=customer.id).pack())
    )
    await call.answer()
    await show(call, "\n\n".join(lines), kb.as_markup())
