"""Commit a guest + order identity before the durable payment/provider attempt.

No caller transaction is committed. Uncertain provider outcomes go to the existing
private status page, not to a fresh checkout. Claim/deletion never frees the identity.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import guest, orders, panel_accounts
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, GuestCheckoutIntent, Order, OrderKind
from paurubot.db.session import get_sessionmaker
from paurubot.payments import order_checkout
from paurubot.payments.base import PaymentError
from paurubot.security import guest_form


def owns(customer: Customer | None, order: Order | None, shop_id: int) -> bool:
    return bool(
        customer is not None
        and customer.shop_id == shop_id
        and customer.is_guest
        and not customer.is_banned
        and customer.guest_token
        and order is not None
        and order.shop_id == shop_id
        and order.customer_id == customer.id
    )


KINDS = ("service", "account")


async def reserve(
    shop_id: int, plan_id: int, method: str, locale: str, token: str, kind: str = "service"
) -> tuple[int, int]:
    if not guest_form.valid(token, shop_id):
        raise guest.GuestError("payment_intent_invalid")
    if kind not in KINDS:
        raise guest.GuestError("guest_plan_unavailable")
    key = guest_form.digest(token)
    async with get_sessionmaker()() as session:
        # A unique insert serializes first requests in PostgreSQL, even before the
        # customer/order exists. Failure before this transaction commits rolls back all three.
        fresh = (
            await session.scalar(
                insert(GuestCheckoutIntent)
                .values(
                    token_digest=key,
                    shop_id=shop_id,
                    plan_id=plan_id,
                    method=method,
                    kind=kind,
                    created_at=utcnow(),
                )
                .on_conflict_do_nothing(index_elements=["token_digest"])
                .returning(GuestCheckoutIntent.token_digest)
            )
        ) is not None
        intent = await session.scalar(
            select(GuestCheckoutIntent).where(GuestCheckoutIntent.token_digest == key).with_for_update()
        )
        if intent is None or (intent.shop_id, intent.plan_id, intent.method, intent.kind) != (
            shop_id,
            plan_id,
            method,
            kind,
        ):
            raise guest.GuestError("payment_intent_conflict")
        if fresh:
            if await guest.shop_for(session, shop_id) is None or method not in await guest.methods(
                session, shop_id
            ):
                raise guest.GuestError("guest_method_unavailable")
            customer = await guest.new_guest(session, shop_id, locale)
            try:
                if kind == "account":
                    account_plan = next(
                        (p for p in await guest.account_plans(session, shop_id) if p.id == plan_id), None
                    )
                    if account_plan is None:
                        raise guest.GuestError("guest_plan_unavailable")
                    draft = await panel_accounts.draft(
                        session, customer, OrderKind.ACCOUNT_PURCHASE, plan=account_plan
                    )
                else:
                    plan = next((p for p in await guest.plans(session, shop_id) if p.id == plan_id), None)
                    if plan is None:
                        raise guest.GuestError("guest_plan_unavailable")
                    draft = await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan)
                order = await orders.create(session, customer, draft, source="web")
            except orders.OrderError as exc:
                raise guest.GuestError(exc.key) from exc
            intent.customer_id, intent.order_id = customer.id, order.id
        else:
            saved_customer = await session.get(Customer, intent.customer_id) if intent.customer_id else None
            saved_order = await session.get(Order, intent.order_id) if intent.order_id else None
            if not owns(saved_customer, saved_order, shop_id):
                raise guest.GuestError("payment_intent_closed")
        assert intent.customer_id is not None and intent.order_id is not None
        result = intent.customer_id, intent.order_id
        await session.commit()
        return result


async def _load(session: AsyncSession, shop_id: int, cid: int, oid: int) -> tuple[Customer, Order]:
    customer = await session.get(Customer, cid, populate_existing=True)
    order = await session.get(Order, oid, populate_existing=True)
    if not owns(customer, order, shop_id):
        raise guest.GuestError("payment_intent_closed")
    assert customer is not None and order is not None
    return customer, order


async def start(
    shop_id: int, plan_id: int, method: str, locale: str, token: str, kind: str = "service"
) -> str:
    cid, oid = await reserve(shop_id, plan_id, method, locale, token, kind)
    async with get_sessionmaker()() as caller:
        customer, order = await _load(caller, shop_id, cid, oid)
        try:
            await order_checkout.start(caller, customer, order, method)
        except (PaymentError, TimeoutError):
            # Reservation/attempt survives. Never replace the order, change method
            # or repeat uncertain remote creation. The read-only page shows evidence.
            pass
        # Do not expose a retired guest token after a concurrent Telegram claim.
        customer, _ = await _load(caller, shop_id, cid, oid)
        return str(customer.guest_token)
