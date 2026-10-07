"""Guest purchase from the web, without a Telegram account (CAT-08).

A visitor picks a plan on /buy, pays at an online gateway and gets the service on a private page
whose address carries a random token. Behind the scenes the visitor is a customer with a
negative placeholder Telegram id, so orders, payments, provisioning and the ledger all work
unchanged. Opening the bot with `start=claim_<token>` later moves everything to the visitor's
real Telegram account.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import catalog, orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, OrderKind, Payment, Plan, Service, Shop, ShopStatus
from paurubot.ledger.accounts import wallet_account, wallet_balance
from paurubot.ledger.posting import transfer
from paurubot.payments import engine as payments
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.gateways import GATEWAYS

# Online methods that finish without a person or a receipt; card transfer and Stars need Telegram.
ONLINE_METHODS = ("zarinpal", *GATEWAYS, "nowpayments", "plisio")
GUEST_ID_BASE = 10**15


class GuestError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(slots=True)
class Checkout:
    customer: Customer
    order: Order
    payment: Payment
    redirect_url: str


async def shop_for(session: AsyncSession, shop_id: int | None) -> Shop | None:
    stmt = select(Shop).where(Shop.status == ShopStatus.ACTIVE)
    stmt = stmt.where(Shop.id == shop_id) if shop_id else stmt.where(Shop.parent_id.is_(None))
    shop = await session.scalar(stmt.order_by(Shop.id).limit(1))
    if shop is None:
        return None
    cfg = await shop_settings.load(session, shop.id)
    return shop if cfg["guest_buy_enabled"] and cfg["bot_enabled"] else None


async def methods(session: AsyncSession, shop_id: int) -> list[str]:
    usable = set(await payments.available(session, shop_id))
    return [name for name in ONLINE_METHODS if name in usable]


async def plans(session: AsyncSession, shop_id: int) -> list[Plan]:
    """Plans a guest may buy: ordinary customer plans delivered by a panel, optionally a chosen few."""
    cfg = await shop_settings.load(session, shop_id)
    stmt = catalog.visible_plans_stmt(shop_id, None).where(Plan.delivery != "stock", Plan.price > 0)
    chosen = [int(p) for p in cfg.get("guest_plan_ids") or [] if str(p).isdigit()]
    if chosen:
        stmt = stmt.where(Plan.id.in_(chosen))
    return list(await session.scalars(stmt.order_by(Plan.sort, Plan.price, Plan.id)))


async def new_guest(session: AsyncSession, shop_id: int, locale: str) -> Customer:
    customer = Customer(
        shop_id=shop_id,
        telegram_id=-(GUEST_ID_BASE + secrets.randbelow(GUEST_ID_BASE)),
        first_name="Guest",
        locale=locale,
        tags=["guest"],
        guest_token=secrets.token_urlsafe(24),
    )
    session.add(customer)
    await session.flush()
    await session.refresh(customer, ["reseller_profile"])
    return customer


async def start(session: AsyncSession, shop_id: int, plan_id: int, method: str, locale: str) -> Checkout:
    if method not in await methods(session, shop_id):
        raise GuestError("guest_method_unavailable")
    plan = next((p for p in await plans(session, shop_id) if p.id == plan_id), None)
    if plan is None:
        raise GuestError("guest_plan_unavailable")
    customer = await new_guest(session, shop_id, locale)
    try:
        order = await orders.create(
            session,
            customer,
            await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan),
            source="web",
        )
        started = await payments.start(session, customer, method, order.amount, order=order)
    except (orders.OrderError, PaymentError) as exc:
        raise GuestError(getattr(exc, "key", "gateway_error")) from exc
    if not started.checkout.redirect_url:
        raise GuestError("guest_method_unavailable")
    started.payment.meta = {**(started.payment.meta or {}), "redirect_url": started.checkout.redirect_url}
    await session.flush()
    return Checkout(customer, order, started.payment, started.checkout.redirect_url)


async def by_token(session: AsyncSession, token: str) -> Customer | None:
    if not token or len(token) > 48:
        return None
    return await session.scalar(select(Customer).where(Customer.guest_token == token))


async def status(session: AsyncSession, guest: Customer) -> dict[str, Any]:
    """The newest purchase of a guest: its order, payment and service, whichever exist."""
    order = await session.scalar(
        select(Order).where(Order.customer_id == guest.id).order_by(Order.id.desc()).limit(1)
    )
    payment = await session.scalar(
        select(Payment).where(Payment.customer_id == guest.id).order_by(Payment.id.desc()).limit(1)
    )
    service = await session.get(Service, order.service_id) if order and order.service_id else None
    return {"order": order, "payment": payment, "service": service}


async def claim(session: AsyncSession, token: str, customer: Customer) -> int:
    """Move a guest's services, orders, payments and any wallet change to a Telegram customer.
    Returns how many services moved; raises GuestError when the token is unknown or used."""
    guest = await by_token(session, token)
    if guest is None or guest.shop_id != customer.shop_id or guest.id == customer.id or customer.is_guest:
        raise GuestError("guest_claim_invalid")
    claimed = await session.execute(
        update(Customer)
        .where(Customer.id == guest.id, Customer.guest_token == token)
        .values(guest_token=None, note=f"claimed by customer #{customer.id}")
    )
    if not claimed.rowcount:  # type: ignore[attr-defined]
        raise GuestError("guest_claim_invalid")
    moved = await session.execute(
        update(Service).where(Service.customer_id == guest.id).values(customer_id=customer.id)
    )
    await session.execute(update(Order).where(Order.customer_id == guest.id).values(customer_id=customer.id))
    await session.execute(
        update(Payment).where(Payment.customer_id == guest.id).values(customer_id=customer.id)
    )
    balance = await wallet_balance(session, guest.shop_id, guest.id)
    if balance > 0:
        source = await wallet_account(session, guest.shop_id, guest.id)
        target = await wallet_account(session, customer.shop_id, customer.id)
        await transfer(
            session,
            shop_id=customer.shop_id,
            source_id=source.id,
            target_id=target.id,
            amount=balance,
            kind="adjustment",
            idempotency_key=f"guest-claim:{guest.id}",
            memo="guest purchase claimed",
            actor=f"customer:{customer.id}",
        )
    customer.total_spent = int(customer.total_spent or 0) + int(guest.total_spent or 0)
    return int(moved.rowcount or 0)  # type: ignore[attr-defined]
