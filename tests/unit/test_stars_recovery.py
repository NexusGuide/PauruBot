"""SEC-F05: a Stars charge is credited however late the bot hears of it, and never twice.

Stars payments shared the gateways' two-day late window, so a successful_payment seen later was
dropped although the customer had paid. Worse, Telegram keeps an undelivered update for only a
day: a bot down longer never saw the charge at all. The bot's own transaction list now settles it.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import update

from paurubot import kv
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine, stars_reconcile
from tests import factories


async def _stars_payment(session, shop, customer, amount: int = 50_000) -> Payment:  # type: ignore[no-untyped-def]
    started = await engine.start(session, customer, "stars", amount)
    await session.commit()
    return started.payment


async def _shop(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"stars_enabled": True, "stars_rate": 1_000})
    await session.commit()
    return shop


async def _expire(session, payment: Payment, days: int) -> None:  # type: ignore[no-untyped-def]
    await session.execute(
        update(Payment)
        .where(Payment.id == payment.id)
        .values(status=PaymentStatus.EXPIRED, expires_at=utcnow() - timedelta(days=days))
    )
    await session.commit()
    await session.refresh(payment)


async def test_a_late_successful_payment_is_still_credited(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    customer = await factories.customer(session, shop)
    payment = await _stars_payment(session, shop, customer)
    await _expire(session, payment, days=5)  # beyond the gateways' two days
    params = {"currency": "XTR", "total_amount": payment.provider_amount, "charge_id": "ch-late"}
    assert await engine.verify(session, payment.id, params)
    await session.commit()
    await engine.fulfill_one(session, payment.id)
    assert await wallet_balance(session, shop.id, customer.id) == 50_000


def _tx(tx_id: str, user_id: int, payload: str, amount: int, kind: str = "invoice_payment"):  # type: ignore[no-untyped-def]
    source = SimpleNamespace(
        type="user", transaction_type=kind, user=SimpleNamespace(id=user_id), invoice_payload=payload
    )
    return SimpleNamespace(id=tx_id, amount=amount, source=source)


class Bot:
    def __init__(self, transactions) -> None:  # type: ignore[no-untyped-def]
        self.transactions = transactions
        self.calls: list[int] = []

    async def get_star_transactions(self, offset: int, limit: int):  # type: ignore[no-untyped-def]
        self.calls.append(offset)
        return SimpleNamespace(transactions=self.transactions[offset : offset + limit])


async def test_the_transaction_list_credits_a_charge_the_bot_never_saw(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    customer = await factories.customer(session, shop)
    other = await factories.customer(session, shop)
    lost = await _stars_payment(session, shop, customer)
    wrong = await _stars_payment(session, shop, customer, 20_000)
    await _expire(session, lost, days=10)
    noise = [_tx(f"gift-{i}", customer.telegram_id, "", 5, kind="gift_purchase") for i in range(120)]
    bot = Bot(
        [
            *noise,  # more than a page of transactions that are not ours to credit
            _tx("ch-lost", customer.telegram_id, f"pay:{lost.id}", lost.provider_amount),
            _tx("ch-wrong", customer.telegram_id, f"pay:{wrong.id}", wrong.provider_amount + 1),
            _tx(
                "ch-other", other.telegram_id, f"pay:{lost.id}", lost.provider_amount
            ),  # paid by someone else
            _tx("ch-twice", customer.telegram_id, f"pay:{lost.id}", lost.provider_amount),  # paid twice
            _tx("ch-unknown", customer.telegram_id, "pay:999999", 10),
        ]
    )
    result = await stars_reconcile.reconcile_shop(session, shop.id, bot)
    assert result.credited == [lost.id]
    assert sorted(result.unmatched) == ["ch-other", "ch-twice", "ch-unknown", "ch-wrong"]
    assert await wallet_balance(session, shop.id, customer.id) == 50_000
    await session.refresh(lost)
    assert lost.status == PaymentStatus.PAID and lost.provider_tx_id == "ch-lost"
    assert bot.calls[:2] == [0, 100]

    # The next pass starts near where this one stopped, credits nothing twice and repeats no report.
    again = await stars_reconcile.reconcile_shop(session, shop.id, bot)
    assert again.credited == [] and again.unmatched == []
    assert bot.calls[-1] == 125 - stars_reconcile.OVERLAP
    assert await wallet_balance(session, shop.id, customer.id) == 50_000
    assert await kv.get_redis().get(f"stars:offset:{shop.id}") == "125"
