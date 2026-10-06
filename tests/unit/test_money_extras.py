"""Bonuses (PAY-14), card auto-approve (PAY-05), gift codes (FIN-03), settlements (PAY-03),
exchange rates (PAY-13), on-chain payments (PAY-11) and hosted crypto invoices (PAY-10)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import select

from paurubot import kv
from paurubot.auth.staff import create_staff
from paurubot.core import gifts, settlements
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import GiftCode, Payment, PaymentPurpose, PaymentStatus, StaffRole
from paurubot.ledger.accounts import wallet_balance
from paurubot.ledger.posting import reconcile
from paurubot.payments import chains, engine, fx, watch
from paurubot.payments.bonus import BonusRuleError, bonus_for, validate_rules
from paurubot.payments.chains import USDT_TRC20, Client, tron_hex, valid_address
from paurubot.payments.providers.crypto import base_amount, display, unique_amount
from paurubot.payments.providers.invoices import NowPaymentsProvider, PlisioProvider, usd_cents
from tests import factories

CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]
TRON = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"


# ---------------------------------------------------------------- bonus rules


def test_bonus_rules_pick_the_best_and_respect_caps() -> None:
    rules = [
        {"provider": "*", "min_amount": 100_000, "percent": 5, "max_bonus": None},
        {"provider": "card", "min_amount": 500_000, "percent": 10, "max_bonus": 40_000},
    ]
    assert bonus_for(rules, "card", 50_000) == 0
    assert bonus_for(rules, "card", 200_000) == 10_000
    assert bonus_for(rules, "card", 1_000_000) == 50_000  # 5% beats the capped 10%
    assert bonus_for(rules, "zarinpal", 600_000) == 30_000
    with pytest.raises(BonusRuleError):
        validate_rules([{"provider": "paypal", "percent": 5}], {"card"})
    with pytest.raises(BonusRuleError):
        validate_rules([{"provider": "card", "percent": 0}], {"card"})


async def test_topup_bonus_is_a_separate_reward(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_accounts": CARDS, "topup_bonus_rules": [{"provider": "card", "min_amount": 0, "percent": 10}]},
    )
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 200_000)
    await engine.attach_receipt(session, started.payment, file_id="f", text="t", image=None)
    await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    await engine.fulfill_one(session, started.payment.id)
    assert await wallet_balance(session, shop.id, cust.id) == 220_000
    assert await reconcile(session, shop.id) == []


# ---------------------------------------------------------------- card auto-approve


async def test_card_auto_approve_needs_history_and_small_amount(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    staff = await create_staff(
        session, shop.id, role=StaffRole.FINANCE, username="fin", password="a-strong-pass-1"
    )
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "card_accounts": CARDS,
            "card_auto_approve": True,
            "card_auto_max_amount": 300_000,
            "card_auto_min_history": 2,
        },
    )
    cust = await factories.customer(session, shop)
    first = await engine.start(session, cust, "card", 100_000)
    await engine.attach_receipt(session, first.payment, file_id="f1", text=None, image=None)
    assert first.payment.status == PaymentStatus.VERIFYING  # no history yet
    for index in range(2):
        session.add(
            Payment(
                shop_id=shop.id,
                customer_id=cust.id,
                purpose=PaymentPurpose.TOPUP,
                provider="card",
                status=PaymentStatus.PAID,
                amount=50_000,
                idempotency_key=f"hist:{index}",
                reviewed_by_staff_id=staff.id,
                provider_tx_id=f"card:hist{index}",
            )
        )
    await session.flush()
    small = await engine.start(session, cust, "card", 200_000)
    await engine.attach_receipt(session, small.payment, file_id="f2", text=None, image=None)
    await session.refresh(small.payment)
    assert small.payment.status == PaymentStatus.PAID and small.payment.review_note == "auto-approved"
    big = await engine.start(session, cust, "card", 400_000)
    await engine.attach_receipt(session, big.payment, file_id="f3", text=None, image=None)
    assert big.payment.status == PaymentStatus.VERIFYING


# ---------------------------------------------------------------- gift codes


async def test_gift_code_redeems_once_per_customer(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    session.add(GiftCode(shop_id=shop.id, code="NOWRUZ", amount=50_000, max_uses=2))
    await session.flush()
    a = await factories.customer(session, shop)
    b = await factories.customer(session, shop, username="b")
    c = await factories.customer(session, shop, username="c")
    assert await gifts.redeem(session, a, "nowruz") == 50_000
    with pytest.raises(gifts.GiftError) as again:
        await gifts.redeem(session, a, "NOWRUZ")
    assert again.value.key == "gift_already_used"
    await gifts.redeem(session, b, "NOWRUZ")
    with pytest.raises(gifts.GiftError) as full:
        await gifts.redeem(session, c, "NOWRUZ")
    assert full.value.key == "gift_used_up"
    assert await wallet_balance(session, shop.id, a.id) == 50_000
    assert await reconcile(session, shop.id) == []


async def test_gift_code_guessing_is_rate_limited(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    for _ in range(gifts.ATTEMPTS_PER_HOUR):
        with pytest.raises(gifts.GiftError):
            await gifts.redeem(session, cust, "WRONG1")
    with pytest.raises(gifts.GiftError) as blocked:
        await gifts.redeem(session, cust, "WRONG2")
    assert blocked.value.key == "gift_too_many_attempts"


# ---------------------------------------------------------------- settlements


async def test_settlement_reduces_outstanding(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 300_000)
    await engine.attach_receipt(session, started.payment, file_id="f", text=None, image=None)
    await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    await engine.fulfill_one(session, started.payment.id)
    await settlements.record(
        session, shop.id, provider="card", amount=200_000, reference="bank-1", note=None, staff_id=None
    )
    [card] = [b for b in await settlements.balances(session, shop.id) if b.provider == "card"]
    assert (card.collected, card.settled, card.outstanding) == (300_000, 200_000, 100_000)
    assert await reconcile(session, shop.id) == []


# ---------------------------------------------------------------- exchange rates


async def test_nobitex_rates_are_parsed_in_toman() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["dstCurrency"] == "rls"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "stats": {"usdt-rls": {"latest": "1050000"}, "trx-rls": {"latest": "300000"}},
            },
        )

    rates = await fx.fetch_nobitex(httpx.MockTransport(handler))
    assert rates == {"usdt": 105_000, "trx": 30_000}
    await fx.store(rates)
    assert await fx.rate({"fx_source": "nobitex", "fx_markup_percent": 2}, "usdt") == 107_100
    with pytest.raises(fx.RateUnavailable):
        await fx.rate({"fx_source": "nobitex"}, "ton")
    assert await fx.rate({"fx_source": "manual", "ton_rate": 400_000}, "ton") == 400_000


async def test_stale_rates_are_ignored() -> None:
    await kv.get_redis().set(
        "fx:rates", json.dumps({"rates": {"usdt": 1}, "at": time.time() - fx.MAX_AGE - 5})
    )
    assert await fx.cached() == {}


# ---------------------------------------------------------------- on-chain


def test_amount_helpers_use_integer_base_units() -> None:
    assert base_amount(105_000, 105_000, 6) == 1_000_000
    assert base_amount(105_001, 105_000, 6) == 1_000_010  # always rounded up
    assert unique_amount(1_000_000, 1, 6) == 1_000_020
    assert unique_amount(1_000_000, 2, 6) != unique_amount(1_000_000, 3, 6)
    assert display(1_000_020, 6) == "1.00002"
    assert display(5_000_000_000, 9) == "5"


def test_tron_addresses_are_checksummed() -> None:
    assert tron_hex(TRON).startswith("41") and len(tron_hex(TRON)) == 42
    assert valid_address("usdt_trc20", TRON)
    assert not valid_address("usdt_trc20", TRON[:-1] + ("a" if TRON[-1] != "a" else "b"))
    assert not valid_address("trx", "0xabc")


def _trc20_transport(transfers: list[dict]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["only_confirmed"] == "true"
        return httpx.Response(200, json={"data": transfers, "success": True})

    return httpx.MockTransport(handler)


async def _crypto_shop(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "crypto_enabled": True,
            "crypto_networks": ["usdt_trc20"],
            "tron_address": TRON,
            "fx_source": "manual",
            "usdt_rate": 100_000,
        },
    )
    return shop


async def test_usdt_payment_is_credited_by_exact_amount(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _crypto_shop(session)
    cust = await factories.customer(session, shop)
    assert "usdt_trc20" in await engine.available(session, shop.id)
    started = await engine.start(session, cust, "usdt_trc20", 200_000)
    await session.commit()
    expected = started.payment.provider_amount
    assert expected and expected > 2_000_000
    now_ms = int(time.time() * 1000)
    transfer = {
        "transaction_id": "a" * 64,
        "to": TRON,
        "type": "Transfer",
        "value": str(expected),
        "block_timestamp": now_ms,
        "token_info": {"address": USDT_TRC20, "decimals": 6},
    }
    decoys = [
        {**transfer, "transaction_id": "b" * 64, "value": str(expected - 1)},  # wrong amount
        {
            **transfer,
            "transaction_id": "c" * 64,
            "token_info": {"address": "TFake", "decimals": 6},
        },  # fake token
    ]
    paid = await watch.scan(session, Client(_trc20_transport([*decoys, transfer])))
    assert paid == 1
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.PAID and started.payment.provider_tx_id == "a" * 64
    await engine.fulfill_one(session, started.payment.id)
    assert await wallet_balance(session, shop.id, cust.id) == 200_000
    # The same transaction can never pay a second time.
    second = await engine.start(session, cust, "usdt_trc20", 200_000)
    second.payment.provider_amount = expected
    await session.commit()
    assert await watch.scan(session, Client(_trc20_transport([transfer]))) == 0


async def test_late_transfer_goes_to_review(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _crypto_shop(session)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "usdt_trc20", 100_000)
    started.payment.status = PaymentStatus.EXPIRED
    await session.commit()
    transfer = {
        "transaction_id": "d" * 64,
        "to": TRON,
        "type": "Transfer",
        "value": str(started.payment.provider_amount),
        "block_timestamp": int(time.time() * 1000),
        "token_info": {"address": USDT_TRC20, "decimals": 6},
    }
    assert await watch.scan(session, Client(_trc20_transport([transfer]))) == 0
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.VERIFYING and started.payment.meta["late_tx"] == "d" * 64
    assert engine.manually_reviewable(started.payment)
    staff = await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="own", password="a-strong-pass-1"
    )
    assert await engine.approve_manual(session, started.payment.id, staff.id)


async def test_ton_transfer_by_comment(session) -> None:  # type: ignore[no-untyped-def]
    client = Client(
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "utime": int(time.time()),
                            "transaction_id": {"lt": "1", "hash": "h"},
                            "in_msg": {"source": "EQsender", "value": "5000000000", "message": "P42"},
                            "out_msgs": [],
                        },
                        {
                            "utime": int(time.time()),
                            "transaction_id": {"lt": "2", "hash": "x"},
                            "in_msg": {"source": "", "value": "0"},
                            "out_msgs": [],
                        },
                    ],
                },
            )
        )
    )
    transfers = await client.incoming("ton", "EQ" + "a" * 46, int(time.time()) - 60)
    assert [(t.tx_id, t.amount, t.memo) for t in transfers] == [("1:h", 5_000_000_000, "P42")]


def test_chain_networks_are_known() -> None:
    assert set(chains.NETWORKS) == {"usdt_trc20", "trx", "ton"}


# ---------------------------------------------------------------- hosted invoices


def test_usd_conversion_rounds_up_to_cents() -> None:
    assert usd_cents(105_000, 105_000) == 100
    assert usd_cents(105_001, 105_000) == 101


async def _invoice_payment(session, provider: str, cents: int):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    payment = Payment(
        shop_id=shop.id,
        customer_id=cust.id,
        purpose=PaymentPurpose.TOPUP,
        provider=provider,
        status=PaymentStatus.PENDING,
        amount=105_000,
        idempotency_key=f"{provider}:test",
        provider_ref="INV-1",
        provider_amount=cents,
        provider_currency="USD",
        expires_at=utcnow(),
    )
    session.add(payment)
    await session.flush()
    return payment


async def test_nowpayments_ipn_signature_and_fields(session) -> None:  # type: ignore[no-untyped-def]
    payment = await _invoice_payment(session, "nowpayments", 100)
    secret = "ipn-secret"
    cfg = {"nowpayments_ipn_secret": secret}
    body = {
        "payment_id": 77,
        "invoice_id": "INV-1",
        "order_id": str(payment.id),
        "payment_status": "finished",
        "price_amount": 1.0,
        "price_currency": "usd",
    }
    raw = json.dumps(body).encode()
    sig = NowPaymentsProvider.signature(raw, secret)
    provider = NowPaymentsProvider()
    ok = await provider.verify(payment, cfg, {"raw": raw, "signature": sig})
    assert ok.paid and ok.provider_tx_id == "np:77"
    assert not (await provider.verify(payment, cfg, {"raw": raw, "signature": "0" * 128})).paid
    tampered = json.dumps({**body, "price_amount": 0.5}).encode()
    resigned = NowPaymentsProvider.signature(tampered, secret)
    assert (
        await provider.verify(payment, cfg, {"raw": tampered, "signature": resigned})
    ).detail == "amount mismatch"
    waiting = json.dumps({**body, "payment_status": "confirming"}).encode()
    result = await provider.verify(
        payment, cfg, {"raw": waiting, "signature": NowPaymentsProvider.signature(waiting, secret)}
    )
    assert not result.paid


async def test_plisio_callback_signature(session) -> None:  # type: ignore[no-untyped-def]
    payment = await _invoice_payment(session, "plisio", 250)
    key = "plisio-key"
    data = {
        "txn_id": "INV-1",
        "order_number": str(payment.id),
        "status": "completed",
        "source_amount": "2.50",
        "source_currency": "USD",
    }
    data["verify_hash"] = hmac.new(
        key.encode(), json.dumps(data, separators=(",", ":")).encode(), hashlib.sha1
    ).hexdigest()
    provider = PlisioProvider()
    result = await provider.verify(payment, {"plisio_api_key": key}, {"raw": json.dumps(data).encode()})
    assert result.paid and result.provider_tx_id == "plisio:INV-1"
    forged = {**data, "status": "completed", "source_amount": "0.01"}
    assert not (
        await provider.verify(payment, {"plisio_api_key": key}, {"raw": json.dumps(forged).encode()})
    ).paid


async def test_payments_table_has_no_duplicate_chain_tx(session) -> None:  # type: ignore[no-untyped-def]
    rows = await session.scalars(select(Payment.provider_tx_id).where(Payment.provider == "usdt_trc20"))
    ids = [r for r in rows if r]
    assert len(ids) == len(set(ids))
