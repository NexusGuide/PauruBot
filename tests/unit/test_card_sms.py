"""REF-02: a bank deposit SMS approves the one card payment it pays for, and nothing else.

Ported as an idea from PasarguardBot dev (forapp SMS webhook), whose invoices can share an amount
and whose parser trusts bank-specific patterns. Here every open card payment asks for a unique
amount while the feature is on, a deposit approves only an exact single match, and a repeated or
unclear message changes nothing.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.api.app import create_app
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, Payment, PaymentStatus
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import bank_sms, engine
from tests import factories

CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]
SECRET = "s3cr3t-forwarder-key-0123456789"


@pytest.mark.parametrize(
    ("text", "rial"),
    [
        ("بانک ملی\nانتقال:1,250,000+\nحساب:0101234567\nمانده:5,000,000\n1403/08/12-14:22", 1_250_000),
        ("بانك ملت\nحساب 1234567890\nواريز 2,500,000\nمانده 9,000,000\n14:30", 2_500_000),
        ("واریز به حساب شما\nمبلغ: 3,000,000 ریال\nمانده: 10,000,000 ریال", 3_000_000),
        ("مبلغ 150,000 تومان به حساب شما واریز شد", 1_500_000),
        ("انتقال:۱٬۲۵۰٬۰۰۰+\nمانده:۸٬۰۰۰٬۰۰۰", 1_250_000),
        ("+2,000,000\nکارت 6037****1234\n1403/08/12\nSIM1_Irancell\ndevice: A52", 2_000_000),
    ],
)
def test_deposits_are_read(text: str, rial: int) -> None:
    found = bank_sms.parse(text)
    assert found is not None and found.amount_rial == rial


@pytest.mark.parametrize(
    "text",
    [
        "برداشت:1,000,000-\nمانده:4,000,000",
        "خرید 200,000 ریال از فروشگاه\nمانده 3,000,000",
        "انتقال:1,000,000+\nانتقال:2,000,000+",  # two deposits in one message: never guess
        "رمز پویا: 123456",
        "کد تایید شما 4821 است\nارسال از +989121234567",
        "مانده حساب شما 7,000,000 ریال",
        "",
    ],
)
def test_anything_unclear_is_not_a_deposit(text: str) -> None:
    assert bank_sms.parse(text) is None


def test_forwarder_amount_only_counts_for_a_declared_deposit() -> None:
    declared = bank_sms.envelope(
        {"text": "unknown layout", "is_deposit": "true", "amount": "50000", "unit": "toman"}
    )
    assert declared is not None and bank_sms.deposit_of(declared) == bank_sms.Deposit(500_000, "forwarder")
    silent = bank_sms.envelope({"text": "unknown layout", "amount": "50000"})
    assert silent is not None and bank_sms.deposit_of(silent) is None


async def _shop(session, **extra):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_accounts": CARDS, "card_sms_enabled": True, "card_sms_secret": SECRET, **extra},
    )
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()
    return shop


async def _card(session, shop, amount: int = 120_000) -> Payment:  # type: ignore[no-untyped-def]
    customer = await factories.customer(session, shop)
    started = await engine.start(session, customer, "card", amount)
    await session.commit()
    return started.payment


async def test_open_card_payments_never_share_an_amount(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    amounts = [(await _card(session, shop)).amount for _ in range(4)]
    assert amounts[0] == 120_000  # the first keeps the exact price
    assert len(set(amounts)) == 4 and all(120_000 <= a <= 120_999 for a in amounts)


async def _post(shop_id: int, body, **kw):  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(), client=("10.2.2.2", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            f"/api/sms/card/{shop_id}", **({"json": body} if body is not None else {}), **kw
        )


async def test_a_deposit_sms_approves_its_payment_once(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    payment = await _card(session, shop)
    sms = {"text": f"بانک ملی\nانتقال:{payment.amount * 10:,}+\nمانده:9,999,000\n14:05", "id": "m-1"}
    assert (
        await _post(shop.id, sms, headers={"x-api-key": "wrong-key-wrong-key-wrong-key"})
    ).status_code == 401
    ok = await _post(shop.id, sms, headers={"x-api-key": SECRET})
    assert ok.status_code == 200 and ok.json() == {"status": "approved"}
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID and payment.review_note == "auto-approved: bank sms"
    assert payment.provider_tx_id == bank_sms.fingerprint(shop.id, bank_sms.envelope(sms))  # type: ignore[arg-type]
    await engine.fulfill_one(session, payment.id)
    assert await wallet_balance(session, shop.id, payment.customer_id) == payment.amount
    # The forwarder retries: the same message is recognised and nothing is credited again.
    again = await _post(shop.id, {**sms, "id": "m-2"}, headers={"authorization": f"Bearer {SECRET}"})
    assert again.json() == {"status": "duplicate"}
    assert await wallet_balance(session, shop.id, payment.customer_id) == payment.amount
    report = await session.scalar(select(Job).where(Job.kind == "card_sms", Job.shop_id == shop.id))
    assert report is not None and report.payload["outcome"] == "approved"


async def test_unclear_deposits_are_reported_not_approved(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    await session.commit()
    # Created before SMS confirmation was on, so both ask for the same amount.
    first, second = await _card(session, shop), await _card(session, shop)
    assert first.amount == second.amount == 120_000
    await shop_settings.set_many(session, shop.id, {"card_sms_enabled": True, "card_sms_secret": SECRET})
    await session.commit()
    headers = {"x-api-key": SECRET}
    ambiguous = await _post(shop.id, {"text": "واریز 1,200,000 ریال"}, headers=headers)
    assert ambiguous.json() == {"status": "ambiguous"}
    unmatched = await _post(shop.id, {"text": "واریز 7,770,000 ریال"}, headers=headers)
    assert unmatched.json() == {"status": "unmatched"}
    other = await _post(shop.id, {"text": "رمز پویا 123456"}, headers=headers)
    assert other.json() == {"status": "not_deposit"}
    for payment in (first, second):
        await session.refresh(payment)
        assert payment.status == PaymentStatus.PENDING
    outcomes = sorted(
        job.payload["outcome"]
        for job in await session.scalars(select(Job).where(Job.kind == "card_sms", Job.shop_id == shop.id))
    )
    assert outcomes == ["ambiguous", "unmatched"]


async def test_forwarder_formats_and_refusals(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    payment = await _card(session, shop, 80_000)
    form = f"text=%2B{payment.amount * 10}&key={SECRET}"
    ok = await _post(
        shop.id, None, content=form, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert ok.json() == {"status": "approved"}
    test = await _post(shop.id, {"text": "test", "test": True, "key": SECRET})
    assert test.json() == {"status": "test"}
    off = await factories.shop(session, "No SMS")
    await session.commit()
    assert (await _post(off.id, {"text": "+10,000", "key": SECRET})).status_code == 401
    assert (await _post(10**9, {"text": "+10,000", "key": SECRET})).status_code == 401
    big = await _post(shop.id, {"text": "x" * 20_000, "key": SECRET})
    assert big.status_code == 413


async def test_the_key_must_exist_and_be_long(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    with pytest.raises(shop_settings.SettingError, match="card_sms_secret_required"):
        await shop_settings.set_many(session, shop.id, {"card_sms_enabled": True})
    with pytest.raises(shop_settings.SettingError, match="card_sms_secret_short"):
        await shop_settings.set_many(session, shop.id, {"card_sms_secret": "short"})
    await shop_settings.set_many(session, shop.id, {"card_sms_secret": SECRET, "card_sms_enabled": True})
    assert (await shop_settings.load(session, shop.id, fresh=True))["card_sms_secret"] == SECRET
