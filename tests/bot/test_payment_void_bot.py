"""REF-01 in the bot: a payment approved by mistake is voided from the customer's payments."""

from __future__ import annotations

from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery

from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import money
from paurubot.db.models import Customer, Payment, PaymentStatus, StaffRole
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine
from tests import factories
from tests.bot import test_admin_customer
from tests.bot.test_admin_customer import FA
from tests.bot.test_admin_users import OWNER, SUPPORT
from tests.fakes.telegram import callback_update

env = test_admin_customer.env
CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]


async def _approved_topup(session, shop, amount: int = 80_000):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS, "card_random_tail": False})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", amount)
    await engine.attach_receipt(session, started.payment, file_id="f", text="receipt", image=None)
    await session.commit()
    assert await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    await session.commit()
    await engine.fulfill_one(session, started.payment.id)
    return cust, started.payment


async def test_owner_voids_from_the_payment_list(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    cust, payment = await _approved_topup(session, shop)
    send, fake = env["send"], env["fake"]
    await send(callback_update(OWNER, Admin(section="upay", id=cust.id).pack()))
    buttons = dict(fake.buttons(fake.last(EditMessageText)))
    target = buttons[FA("adm_void_button", id=payment.id)]
    await send(callback_update(OWNER, target))
    card = fake.last(EditMessageText)
    assert FA("adm_void_card", id=payment.id, amount=money(payment.amount), provider="card") == card.text
    reason = dict(fake.buttons(card))[FA("void_reason_fake_receipt")]
    await send(callback_update(OWNER, reason))
    confirm = fake.last(EditMessageText)
    labels = dict(fake.buttons(confirm))
    assert FA("adm_void_do") in labels
    await send(callback_update(OWNER, labels[FA("adm_void_do_block")]))
    done = fake.last(EditMessageText)
    assert FA("adm_void_done", id=payment.id) in done.text and FA("adm_void_done_blocked") in done.text
    await session.refresh(payment)
    customer = await session.get(Customer, cust.id, populate_existing=True)
    assert payment.status == PaymentStatus.VOIDED and payment.meta["void"]["reason"] == "fake_receipt"
    assert customer is not None and customer.card_trust == "blocked"
    assert await wallet_balance(session, shop.id, cust.id) == 0
    # Pressing an old button again changes nothing.
    await send(callback_update(OWNER, labels[FA("adm_void_do")]))
    assert env["fake"].last(EditMessageText).text == done.text


async def test_a_refused_void_explains_why(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    cust, payment = await _approved_topup(session, shop)
    await wallet.adjust(session, shop.id, cust.id, -50_000, key="spent", actor="test")
    await session.commit()
    send, fake = env["send"], env["fake"]
    await send(callback_update(OWNER, Admin(section="pvoid", action="c00", id=payment.id).pack()))
    text = fake.last(EditMessageText).text
    assert text == FA("void_wallet_spent", balance=money(30_000), amount=money(80_000))
    row = await session.get(Payment, payment.id, populate_existing=True)
    assert row is not None and row.status == PaymentStatus.PAID


async def test_void_needs_the_refund_permission(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.auth import rbac
    from paurubot.bot.routers.admin_customer import router

    shop = env["shop"]
    cust, payment = await _approved_topup(session, shop)
    support = StaffIdentity(StaffRole.SUPPORT, None, frozenset(rbac.permissions_for(StaffRole.SUPPORT)))
    handler = next(h for h in router.callback_query.handlers if h.callback.__name__ == "void_payment")
    call = callback_update(SUPPORT, Admin(section="pvoid", action="c00", id=payment.id).pack()).callback_query
    assert isinstance(call, CallbackQuery)
    ok, _ = await handler.check(call, staff=support)
    assert not ok
    # A staff member who can read payments but not refund sees no void button either.
    viewer = StaffIdentity(StaffRole.SUPPORT, None, frozenset({"customers.read", "payments.read"}))
    history = next(h for h in router.callback_query.handlers if h.callback.__name__ == "payment_history")
    call = callback_update(SUPPORT, Admin(section="upay", id=cust.id).pack()).callback_query
    ok, _ = await history.check(call, staff=viewer)
    assert ok
