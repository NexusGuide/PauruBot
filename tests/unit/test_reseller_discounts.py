"""Discount codes created and used in a reseller's own sales bot."""

from __future__ import annotations

from datetime import timedelta

from paurubot.core import orders, resale
from paurubot.db.base import utcnow
from paurubot.db.models import DiscountCode, DiscountKind, OrderKind, OrderStatus
from paurubot.ledger import wallet
from sqlalchemy import select
from tests import factories
from tests.unit.test_provisioning import drain
from tests.unit.test_reseller_bot import TOKEN
from tests.unit.test_reseller_bot import env as env  # noqa: F401


async def test_reseller_discount_never_crosses_wholesale_floor(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    code = DiscountCode(
        shop_id=child.id,
        code="VIP90",
        kind=DiscountKind.PERCENT,
        value=90,
        max_uses=1,
        per_customer_limit=1,
        starts_at=utcnow(),
    )
    session.add(code)
    await session.flush()
    buyer = await factories.customer(session, child, telegram_id=63110)
    draft = await orders.draft(session, buyer, OrderKind.PURCHASE, plan=env["plan"])
    order = await orders.create(session, buyer, draft, code=code)
    assert order.params["wholesale"] == 80_000
    assert order.amount == 80_000
    await wallet.topup(session, child.id, buyer.id, 80_000, provider="card", payment_id=63110)
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    saved = await session.scalar(select(DiscountCode).where(DiscountCode.id == code.id))
    assert saved is not None and saved.used_count == 1


async def test_reseller_discount_expiry_is_enforced(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    code = DiscountCode(
        shop_id=child.id,
        code="OLD10",
        kind=DiscountKind.PERCENT,
        value=10,
        expires_at=utcnow() - timedelta(minutes=1),
    )
    session.add(code)
    await session.flush()
    buyer = await factories.customer(session, child, telegram_id=63111)
    draft = await orders.draft(session, buyer, OrderKind.PURCHASE, plan=env["plan"])
    try:
        await orders.create(session, buyer, draft, code=code)
    except Exception as exc:
        assert getattr(exc, "key", None) == "discount_expired"
    else:
        raise AssertionError("expired reseller discount was accepted")
