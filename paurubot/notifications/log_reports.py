"""Full reports to the shop's report group or channel (SUP-03): every field staff look for, in the
shop's own language, and a button that opens the customer's card in the bot.

Each report goes to its topic's chat (a group, a forum thread or a channel) set under Settings →
Reports; a topic without one of its own falls back to a broader one, then to "all".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.formatting import days_left, esc, jdate, money, size
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    DiscountCode,
    Lottery,
    Order,
    OrderKind,
    Panel,
    Payment,
    Service,
    Shop,
    Staff,
)
from paurubot.i18n import Texts, texts_for

GB = 1024**3


async def shop_texts(session: AsyncSession, shop_id: int) -> Texts:
    """Reports are read by the shop's staff: always in the shop's language."""
    shop = await session.get(Shop, shop_id)
    return await texts_for(session, shop_id, shop.default_locale if shop else None)


async def manage_markup(
    session: AsyncSession, shop_id: int, customer: Customer
) -> InlineKeyboardMarkup | None:
    """«Manage user»: opens this customer's card in a private chat with the bot (staff only there).
    A report sits in a group, where an in-place admin screen would be shown to everyone."""
    shop = await session.get(Shop, shop_id)
    if shop is None or not shop.bot_username or customer.telegram_id <= 0:
        return None
    t = await shop_texts(session, shop_id)
    url = f"https://t.me/{shop.bot_username}?start=cust_{customer.id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("report_btn_manage"), url=url)]]
    )


def user_block(t: Texts, customer: Customer) -> str:
    return t(
        "report_user_block",
        name=esc(customer.display_name),
        username=f"@{esc(customer.username)}" if customer.username else t("report_none"),
        telegram_id=customer.telegram_id,
        phone=esc(customer.phone) if customer.phone else t("report_none"),
    )


def now_text() -> str:
    return jdate(utcnow(), with_time=True)


def _volume(t: Texts, gb: int) -> str:
    return t("report_gb", value=gb) if gb > 0 else t("report_unlimited")


def _duration(t: Texts, days: int) -> str:
    return t("report_days", value=days) if days > 0 else t("report_unlimited")


def method_name(t: Texts, provider: str | None) -> str:
    if not provider:
        return t("report_none")
    label = t(f"btn_pay_{provider}")
    if label == f"btn_pay_{provider}":
        return esc(provider)
    return esc(re.sub(r"^[^\w]+", "", label).strip() or label)  # the button's emoji stays on the button


async def _reviewer(session: AsyncSession, t: Texts, payment: Payment) -> str:
    if payment.reviewed_by_staff_id is None:
        return t("report_reviewer_auto") if payment.provider == "card" else t("report_reviewer_gateway")
    staff = await session.get(Staff, payment.reviewed_by_staff_id)
    if staff is None:
        return t("report_none")
    name = esc(staff.display_name or staff.username or f"#{staff.id}")
    if staff.telegram_id:
        return f"{name} (<code>{staff.telegram_id}</code>)"
    return name


# --- the reports ------------------------------------------------------------------------------


async def new_user(session: AsyncSession, customer: Customer) -> tuple[str, InlineKeyboardMarkup | None]:
    t = await shop_texts(session, customer.shop_id)
    referrer = await session.get(Customer, customer.referrer_id) if customer.referrer_id else None
    text = t(
        "report_joined",
        user=user_block(t, customer),
        referrer=(
            f"{esc(referrer.display_name)} (<code>{referrer.telegram_id}</code>)"
            if referrer
            else t("report_none")
        ),
        time=now_text(),
    )
    return text, await manage_markup(session, customer.shop_id, customer)


async def order_report(
    session: AsyncSession,
    order: Order,
    customer: Customer,
    *,
    config: str,
    panel: Panel | None,
    gb: int,
    days: int,
    plan_title: str | None = None,
) -> tuple[str, str, InlineKeyboardMarkup | None]:
    """(topic, text, markup) for a completed order; a free trial has its own topic."""
    t = await shop_texts(session, order.shop_id)
    method = await _order_method(session, order)
    common = {
        "user": user_block(t, customer),
        "config": esc(config),
        "panel": esc(panel.name) if panel is not None else t("report_none"),
        "volume": _volume(t, gb),
        "duration": _duration(t, days),
        "time": now_text(),
    }
    markup = await manage_markup(session, order.shop_id, customer)
    if order.kind == OrderKind.TRIAL:
        return "trials", t("report_trial", **common), markup
    discount = ""
    if order.discount_code_id or order.code_discount:
        code = await session.get(DiscountCode, order.discount_code_id) if order.discount_code_id else None
        discount = t(
            "report_discount",
            code=esc(code.code) if code is not None else t("report_none"),
            amount=money(int(order.code_discount or 0)),
        )
    text = t(
        "report_purchase",
        kind=t(f"order_kind_{order.kind.value}"),
        order=order.id,
        plan=esc(plan_title or (order.plan.title if order.plan else "")) or t("report_none"),
        amount=money(order.amount),
        discount=discount,
        method=method,
        **common,
    )
    return "orders", text, markup


async def _order_method(session: AsyncSession, order: Order) -> str:
    from sqlalchemy import select

    from paurubot.db.models import PaymentStatus

    t = await shop_texts(session, order.shop_id)
    if order.amount <= 0:
        return t("report_free")
    provider = await session.scalar(
        select(Payment.provider)
        .where(Payment.order_id == order.id, Payment.status == PaymentStatus.PAID)
        .order_by(Payment.id.desc())
        .limit(1)
    )
    return method_name(t, provider or "wallet")


async def service_order(
    session: AsyncSession, order: Order, service: Service, customer: Customer
) -> tuple[str, str, InlineKeyboardMarkup | None]:
    panel = await session.get(Panel, service.panel_id) if service.panel_id else None
    params = order.params or {}
    gb = int(service.data_limit // GB) if service.data_limit else 0
    days = days_left(service.expire_at) or 0
    if order.kind == OrderKind.TRIAL:
        gb, days = int(params.get("gb") or gb), int(params.get("days") or days)
    elif order.plan is not None and order.kind in {OrderKind.PURCHASE, OrderKind.RENEW, OrderKind.UPGRADE}:
        gb, days = int(order.plan.data_gb), int(order.plan.days)
    elif order.kind == OrderKind.EXTRA_VOLUME:
        gb, days = int(params.get("extra_gb") or 0), 0
    elif order.kind == OrderKind.EXTRA_DAYS:
        gb, days = 0, int(params.get("extra_days") or 0)
    custom = params.get("custom") if isinstance(params.get("custom"), dict) else None
    if custom:
        gb, days = int(custom.get("gb") or 0), int(custom.get("days") or 0)
    return await order_report(
        session,
        order,
        customer,
        config=service.panel_username or "",
        panel=panel,
        gb=gb,
        days=days,
        plan_title=order.plan.title if order.plan else None,
    )


async def payment_paid(session: AsyncSession, payment: Payment, customer: Customer) -> str:
    t = await shop_texts(session, payment.shop_id)
    return t(
        "report_paid",
        id=payment.id,
        method=method_name(t, payment.provider),
        amount=money(payment.amount),
        purpose=await _purpose(session, t, payment),
        reviewer=await _reviewer(session, t, payment),
        time=jdate(payment.paid_at or utcnow(), with_time=True),
        user=user_block(t, customer),
    )


async def payment_rejected(session: AsyncSession, payment: Payment, customer: Customer, reason: str) -> str:
    t = await shop_texts(session, payment.shop_id)
    return t(
        "report_rejected",
        id=payment.id,
        method=method_name(t, payment.provider),
        amount=money(payment.amount),
        purpose=await _purpose(session, t, payment),
        reviewer=await _reviewer(session, t, payment),
        reason=esc(reason) or t("report_none"),
        time=jdate(payment.reviewed_at or utcnow(), with_time=True),
        user=user_block(t, customer),
    )


async def _purpose(session: AsyncSession, t: Texts, payment: Payment) -> str:
    if payment.order_id:
        found = await session.get(Order, payment.order_id)
        if found is not None:
            return t("report_purpose_order", kind=t(f"order_kind_{found.kind.value}"), order=found.id)
    return t("report_purpose_topup")


async def referral_paid(
    session: AsyncSession,
    *,
    buyer: Customer,
    referrer: Customer,
    order: Order | None,
    reward: int,
    percent: int,
    balance_after: int,
) -> str:
    t = await shop_texts(session, referrer.shop_id)
    return t(
        "report_referral",
        order=order.id if order is not None else "-",
        order_amount=money(order.amount if order is not None else 0),
        reward=money(reward),
        percent=percent,
        before=money(balance_after - reward),
        after=money(balance_after),
        buyer=user_block(t, buyer),
        referrer=user_block(t, referrer),
        time=now_text(),
    )


async def service_deleted(session: AsyncSession, service: Service, customer: Customer) -> str:
    """The cleanup removed a service that stayed expired or out of data (the «cron» report)."""
    t = await shop_texts(session, service.shop_id)
    panel = await session.get(Panel, service.panel_id) if service.panel_id else None
    out_of_data = service.limited_at is not None or (
        service.data_limit > 0 and service.used_bytes >= service.data_limit
    )
    left = max(0, int(service.data_limit) - int(service.used_bytes)) if service.data_limit else None
    return t(
        "report_service_deleted",
        config=esc(service.panel_username or ""),
        status=t("report_status_traffic") if out_of_data else t("report_status_time"),
        days=max(0, days_left(service.expire_at) or 0),
        left=size(left) if left is not None else t("report_unlimited"),
        online=_when(service.online_at, t),
        panel=esc(panel.name) if panel is not None else t("report_none"),
        user=user_block(t, customer),
    )


def _when(value: datetime | None, t: Texts) -> str:
    return jdate(value, with_time=True) if value else t("report_never")


async def order_failed(session: AsyncSession, order: Order, customer: Customer, reason: str) -> str:
    """A purchase, renewal or trial the panel refused: what the customer wanted, the panel's own
    error, and where the money went."""
    t = await shop_texts(session, order.shop_id)
    panel_id = (order.params or {}).get("panel_id") or (order.plan.panel_id if order.plan else None)
    panel = await session.get(Panel, int(panel_id)) if panel_id else None
    refund = (
        t("report_failed_refund", amount=money(order.amount))
        if order.amount > 0 and order.kind != OrderKind.TRIAL
        else ""
    )
    return t(
        "report_failed",
        kind=t(f"order_kind_{order.kind.value}"),
        reason=esc(reason[:1500]) or t("report_none"),
        order=order.id,
        refund=refund,
        panel=esc(panel.name) if panel is not None else t("report_none"),
        user=user_block(t, customer),
        time=now_text(),
    )


# --- lotteries (GRW-04) -----------------------------------------------------------------------

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
AWARDS_PER_MESSAGE = 8


def person_name(customer: Customer) -> str:
    """The customer's own name where they gave one; their @username or ID otherwise."""
    return (customer.first_name or "").strip() or customer.display_name


def rank_mark(rank: int) -> str:
    return MEDALS.get(rank, "🏅")


def prize_text(t: Texts, prize: dict[str, Any]) -> str:
    kind, value, label = prize.get("kind"), int(prize.get("value") or 0), str(prize.get("label") or "")
    if kind == "wallet":
        text = t("lot_prize_wallet", amount=money(value))
    elif kind == "service":
        text = t("lot_prize_service", gb=value, days=int(prize.get("days") or 0))
    else:
        return esc(label) or t("report_none")
    return f"{text} ({esc(label)})" if label else text


def score_text(t: Texts, mode: str, points: int, purchases: int, spent: int) -> str:
    key = {"points": "lot_score_points", "purchases": "lot_score_purchases"}.get(mode, "lot_score_join")
    return t(key, points=points, purchases=purchases, spent=money(spent))


def award_status(t: Texts, award: dict[str, Any]) -> str:
    status = str(award.get("status") or "manual")
    return t(f"report_lottery_st_{status}", service=esc(str(award.get("service") or "")))


def mode_text(t: Texts, lottery: Lottery) -> str:
    return t(f"adm_grw_mode_{lottery.mode}", per=money(lottery.points_per))


def lottery_awards(lottery: Lottery) -> list[dict[str, Any]]:
    """The draw's awards; a lottery drawn before ranked prizes only lists its winners."""
    result = lottery.result or {}
    if "awards" in result:
        return [dict(item) for item in result["awards"]]
    prize = {"kind": "text", "label": lottery.prize, "value": 0, "status": "manual"}
    return [
        {"rank": rank, "customer_id": int(cid), "points": 0, "purchases": 0, "spent": 0, **prize}
        for rank, cid in enumerate(result.get("winners", []), start=1)
    ]


async def current_awards(session: AsyncSession, lottery: Lottery) -> list[dict[str, Any]]:
    """The awards with each service prize's progress read from its order: being made, made
    (with the new config's name) or failed, which staff then hand over themselves."""
    from paurubot.db.models import OrderStatus

    awards = lottery_awards(lottery)
    for award in awards:
        if award.get("kind") != "service" or not award.get("order_id"):
            continue
        order = await session.get(Order, int(award["order_id"]))
        if order is None or order.status in {OrderStatus.FAILED, OrderStatus.CANCELED, OrderStatus.REFUNDED}:
            award["status"] = "failed"
        elif order.status == OrderStatus.COMPLETED:
            service = await session.get(Service, order.service_id) if order.service_id else None
            award["status"], award["service"] = "applied", service.panel_username if service else ""
    return awards


async def lottery_result(
    session: AsyncSession, lottery: Lottery
) -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """The draw's full report for staff: every winner with their prize, what became of it, who
    they are and their score, and a button to each winner's card. Long lists span messages."""
    t = await shop_texts(session, lottery.shop_id)
    shop = await session.get(Shop, lottery.shop_id)
    head = t(
        "report_lottery_head",
        title=esc(lottery.title),
        mode=mode_text(t, lottery),
        pick=t(f"adm_grw_pick_{lottery.pick}"),
        entrants=int((lottery.result or {}).get("entrants") or 0),
        time=now_text(),
    )
    awards = await current_awards(session, lottery)
    if not awards:
        return [(f"{head}\n\n{t('report_lottery_none')}", None)]
    messages: list[tuple[str, InlineKeyboardMarkup | None]] = []
    for start in range(0, len(awards), AWARDS_PER_MESSAGE):
        blocks, buttons = [], []
        for award in awards[start : start + AWARDS_PER_MESSAGE]:
            customer = await session.get(Customer, int(award["customer_id"]))
            if customer is None:
                continue
            rank = t("lot_rank", rank=award["rank"])
            blocks.append(
                t(
                    "report_lottery_award",
                    mark=rank_mark(int(award["rank"])),
                    rank=rank,
                    prize=prize_text(t, award),
                    status=award_status(t, award),
                    user=user_block(t, customer),
                    score=score_text(
                        t,
                        lottery.mode,
                        int(award.get("points") or 0),
                        int(award.get("purchases") or 0),
                        int(award.get("spent") or 0),
                    ),
                )
            )
            if shop is not None and shop.bot_username and customer.telegram_id > 0:
                url = f"https://t.me/{shop.bot_username}?start=cust_{customer.id}"
                label = t("report_lottery_btn", rank=rank, name=person_name(customer)[:32])
                buttons.append([InlineKeyboardButton(text=label, url=url)])
        top = head if start == 0 else t("report_lottery_more", title=esc(lottery.title))
        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        messages.append(("\n\n".join([top, *blocks]), markup))
    return messages


async def bot_owner(session: AsyncSession, shop: Shop) -> tuple[int | None, Customer | None]:
    """The shop a child bot was sold from, and the customer who owns it: a reseller's bot or a
    customer's own bot. (None, None) for a shop that is not a child bot."""
    from paurubot.db.models import PanelAccount, ResellerProfile

    if shop.reseller_profile_id is not None:
        profile = await session.get(ResellerProfile, shop.reseller_profile_id)
        if profile is not None:
            return profile.shop_id, await session.get(Customer, profile.customer_id)
    if shop.own_account_id is not None:
        account = await session.get(PanelAccount, shop.own_account_id)
        if account is not None:
            return account.shop_id, await session.get(Customer, account.customer_id)
    return None, None


async def bot_token_rejected(
    session: AsyncSession, shop: Shop
) -> tuple[int, str, InlineKeyboardMarkup | None] | None:
    """Which bot Telegram stopped accepting, and whose: for the parent shop's reports group."""
    parent_id, owner = await bot_owner(session, shop)
    if parent_id is None:
        return None
    t = await shop_texts(session, parent_id)
    text = t(
        "report_bot_token_rejected",
        bot=f"@{esc(shop.bot_username)}" if shop.bot_username else t("report_none"),
        title=esc(shop.title),
        user=user_block(t, owner) if owner is not None else t("report_none"),
    )
    markup = await manage_markup(session, parent_id, owner) if owner is not None else None
    return parent_id, text, markup
