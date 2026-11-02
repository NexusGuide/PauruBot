"""Individual customer navigation, shop-scoped payment history and voiding a payment (REF-01)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin_hub import USER_FILTERS, user_lists
from paurubot.bot.utils import StaffOnly, clear_customer_input, show
from paurubot.core import customers
from paurubot.core.formatting import esc, money
from paurubot.db.models import Payment, PaymentStatus
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.payments import voids

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
    staff: StaffIdentity,
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
    if staff.can("payments.refund"):
        for row in rows:
            if voids.voidable(row):
                kb.row(
                    InlineKeyboardButton(
                        text=t("adm_void_button", id=row.id),
                        callback_data=Admin(section="pvoid", id=row.id, page=page).pack(),
                    )
                )
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


async def _voidable(
    call: CallbackQuery, session: AsyncSession, shop: ShopRow, t: Texts, payment_id: int
) -> Payment | None:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.shop_id != shop.id:
        await call.answer(t("void_not_found"), show_alert=True)
        return None
    if not voids.voidable(payment):
        await call.answer(
            t("void_already" if payment.status == PaymentStatus.VOIDED else "void_not_voidable"),
            show_alert=True,
        )
        return None
    return payment


@router.callback_query(Admin.filter(F.section == "pvoid"), StaffOnly("payments.refund"))
async def void_payment(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Void a payment approved by mistake: pick a reason, confirm, then take it back (REF-01).

    Actions: ``show`` the card, ``r<n>`` pick reason n, ``c<n><b>`` void with reason n
    (b=1 also blocks card payments). Every step re-checks the shop and the payment.
    """
    payment = await _voidable(call, session, shop, t, callback_data.id)
    if payment is None:
        return
    action = callback_data.action
    back = Admin(section="upay", id=payment.customer_id, page=callback_data.page).pack()
    kb = InlineKeyboardBuilder()
    if action == "show":
        for index, reason in enumerate(voids.REASONS):
            kb.row(
                InlineKeyboardButton(
                    text=t(f"void_reason_{reason}"),
                    callback_data=Admin(
                        section="pvoid", action=f"r{index}", id=payment.id, page=callback_data.page
                    ).pack(),
                )
            )
        kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back))
        await call.answer()
        await show(
            call,
            t("adm_void_card", id=payment.id, amount=money(payment.amount), provider=esc(payment.provider)),
            kb.as_markup(),
        )
        return
    index = int(action[1]) if len(action) >= 2 and action[1].isdigit() else -1
    if not 0 <= index < len(voids.REASONS) or action[0] not in "rc":
        await call.answer()
        return
    reason = voids.REASONS[index]
    if action[0] == "r":
        for block, label in ((0, "adm_void_do"), (1, "adm_void_do_block")):
            kb.row(
                InlineKeyboardButton(
                    text=t(label),
                    callback_data=Admin(
                        section="pvoid", action=f"c{index}{block}", id=payment.id, page=callback_data.page
                    ).pack(),
                )
            )
        kb.row(
            InlineKeyboardButton(
                text=t("btn_back"),
                callback_data=Admin(section="pvoid", id=payment.id, page=callback_data.page).pack(),
            )
        )
        await call.answer()
        await show(
            call,
            t(
                "adm_void_confirm",
                id=payment.id,
                amount=money(payment.amount),
                reason=t(f"void_reason_{reason}"),
            ),
            kb.as_markup(),
        )
        return
    block_card = action[2:3] == "1"
    try:
        result = await voids.void(
            session,
            payment.id,
            shop_id=shop.id,
            actor=Actor("staff", staff.id),
            reason=reason,
            block_card=block_card,
        )
    except voids.VoidError as exc:
        await session.rollback()
        params = {
            key: money(value) if key in {"balance", "amount"} else value for key, value in exc.params.items()
        }
        await call.answer()
        kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back))
        await show(call, t(exc.key, **params), kb.as_markup())
        return
    await session.commit()
    lines = [t("adm_void_done", id=payment.id)]
    if result.service_id:
        lines.append(t("adm_void_done_service", service=result.service_id))
    if result.kept:
        lines.append(
            t("adm_void_done_kept", items=", ".join(t(f"adm_void_kept_{item}") for item in result.kept))
        )
    if block_card:
        lines.append(t("adm_void_done_blocked"))
    kb.row(InlineKeyboardButton(text=t("btn_back"), callback_data=back))
    await call.answer()
    await show(call, "\n\n".join(lines), kb.as_markup())
