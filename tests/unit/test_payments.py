from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.config import get_settings
from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Job, OrderKind, OrderStatus, Payment, PaymentStatus
from paurubot.ledger.accounts import wallet_balance
from paurubot.ledger.posting import reconcile
from paurubot.payments import engine, receipts
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.zarinpal import ZarinpalProvider
from tests import factories

CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]


def _receipt(text: str = "receipt 123", scale: int = 1) -> bytes:
    img = Image.new("RGB", (300 * scale, 200 * scale), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20 * scale, 20 * scale, 200 * scale, 120 * scale], fill="black")
    draw.text((30 * scale, 150 * scale), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def _shop_with_cards(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    return shop


async def test_card_topup_review_and_fulfillment(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop_with_cards(session)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 100_000)
    assert started.checkout.instructions["accounts"][0]["number"].startswith("6037")
    await engine.attach_receipt(session, started.payment, file_id="file-1", text=None, image=_receipt())
    assert started.payment.status == PaymentStatus.VERIFYING
    assert await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    assert not await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    result = await engine.fulfill_one(session, started.payment.id)
    assert result is not None
    assert await wallet_balance(session, shop.id, cust.id) == 100_000
    assert await engine.fulfill_one(session, started.payment.id) is None  # exactly once
    assert await reconcile(session, shop.id) == []


async def test_direct_pay_for_order_settles_and_queues_provisioning(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop_with_cards(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=150_000)
    cust = await factories.customer(session, shop)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    started = await engine.pay_order(session, cust, order, "card")
    assert started.payment.amount == 150_000
    await engine.attach_receipt(session, started.payment, file_id="f", text="paid", image=None)
    await engine.approve_card(session, started.payment.id, staff_id=None)  # type: ignore[arg-type]
    result = await engine.fulfill_one(session, started.payment.id)
    assert result is not None and result.order_settled
    await session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await wallet_balance(session, shop.id, cust.id) == 0
    job = await session.scalar(select(Job).where(Job.dedupe_key == f"order:{order.id}:provision"))
    assert job is not None


async def test_bounds_and_disabled_methods(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    with pytest.raises(PaymentError, match="method_disabled"):
        await engine.start(session, cust, "card", 100_000)  # no card accounts configured
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    with pytest.raises(PaymentError, match="amount_out_of_range"):
        await engine.start(session, cust, "card", 1_000)
    assert await engine.available(session, shop.id) == ["card"]


async def test_duplicate_receipts_are_flagged(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop_with_cards(session)
    a = await factories.customer(session, shop)
    b = await factories.customer(session, shop)
    first = await engine.start(session, a, "card", 50_000)
    await engine.attach_receipt(session, first.payment, file_id="f1", text=None, image=_receipt())
    second = await engine.start(session, b, "card", 50_000)
    dup = await engine.attach_receipt(session, second.payment, file_id="f2", text=None, image=_receipt())
    assert dup is not None and dup.id == first.payment.id
    # a re-encoded, resized copy is caught by the perceptual hash
    third = await engine.start(session, b, "card", 50_000)
    resized = Image.open(io.BytesIO(_receipt())).resize((450, 300))
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    near = await engine.attach_receipt(session, third.payment, file_id="f3", text=None, image=buf.getvalue())
    assert near is not None
    other = await engine.start(session, b, "card", 50_000)
    different = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(different).ellipse([100, 20, 290, 190], fill="black")
    buf2 = io.BytesIO()
    different.save(buf2, format="PNG")
    assert (
        await engine.attach_receipt(session, other.payment, file_id="f4", text=None, image=buf2.getvalue())
        is None
    )


async def test_duplicate_block_rejects_exact_copy(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop_with_cards(session)
    await shop_settings.set_many(session, shop.id, {"receipt_duplicate_block": True})
    cust = await factories.customer(session, shop)
    first = await engine.start(session, cust, "card", 50_000)
    await engine.attach_receipt(session, first.payment, file_id="f1", text=None, image=_receipt("x"))
    second = await engine.start(session, cust, "card", 50_000)
    await engine.attach_receipt(session, second.payment, file_id="f2", text=None, image=_receipt("x"))
    await session.refresh(second.payment)
    assert second.payment.status == PaymentStatus.FAILED


def test_dhash_is_stable() -> None:
    a, b = receipts.dhash(_receipt()), receipts.dhash(_receipt())
    assert a and b and receipts.distance(a, b) == 0
    assert receipts.dhash(b"not an image") is None


def _zarinpal(code_request: int = 100, code_verify: int = 100, ref_id: int = 777):  # type: ignore[no-untyped-def]
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path.endswith("request.json"):
            return httpx.Response(
                200, json={"data": {"code": code_request, "authority": "A0001"}, "errors": []}
            )
        return httpx.Response(200, json={"data": {"code": code_verify, "ref_id": ref_id}, "errors": []})

    return ZarinpalProvider(httpx.MockTransport(handler)), seen


async def _zarinpal_shop(session, monkeypatch, provider):  # type: ignore[no-untyped-def]
    monkeypatch.setitem(engine.PROVIDERS, "zarinpal", provider)
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"zarinpal_enabled": True, "zarinpal_merchant": "m-123"})
    return shop


async def test_zarinpal_happy_path_and_replay(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider, seen = _zarinpal()
    shop = await _zarinpal_shop(session, monkeypatch, provider)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "zarinpal", 80_000)
    assert started.checkout.redirect_url and started.checkout.redirect_url.endswith("/StartPay/A0001")
    assert seen[0]["callback_url"].startswith("https://bot.example.com/api/pay/zarinpal/callback")
    assert not await engine.verify(session, started.payment.id, {"Authority": "WRONG", "Status": "OK"})
    assert await engine.verify(session, started.payment.id, {"Authority": "A0001", "Status": "OK"})
    await engine.fulfill_one(session, started.payment.id)
    assert await wallet_balance(session, shop.id, cust.id) == 80_000
    # a second payment verified with the same gateway reference is a replay
    again = await engine.start(session, cust, "zarinpal", 80_000)
    assert not await engine.verify(session, again.payment.id, {"Authority": "A0001", "Status": "OK"})
    await session.refresh(again.payment)
    assert again.payment.status == PaymentStatus.DISPUTED
    assert await wallet_balance(session, shop.id, cust.id) == 80_000


async def test_zarinpal_cancel_and_verify_failure(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider, _ = _zarinpal(code_verify=-51)
    shop = await _zarinpal_shop(session, monkeypatch, provider)
    cust = await factories.customer(session, shop)
    one = await engine.start(session, cust, "zarinpal", 80_000)
    assert not await engine.verify(session, one.payment.id, {"Authority": "A0001", "Status": "NOK"})
    await session.refresh(one.payment)
    assert one.payment.status == PaymentStatus.FAILED
    two = await engine.start(session, cust, "zarinpal", 80_000)
    assert not await engine.verify(session, two.payment.id, {"Authority": "A0001", "Status": "OK"})
    assert await wallet_balance(session, shop.id, cust.id) == 0


async def test_stars_amount_must_match(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"stars_enabled": True, "stars_rate": 2_000})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "stars", 50_000)
    assert started.checkout.instructions["stars"] == 25
    pid = started.payment.id
    assert not await engine.verify(session, pid, {"currency": "XTR", "total_amount": 1, "charge_id": "c1"})
    assert await engine.verify(session, pid, {"currency": "XTR", "total_amount": 25, "charge_id": "c1"})


async def test_payment_expiry(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop_with_cards(session)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 50_000)
    started.payment.expires_at = utcnow() - timedelta(minutes=1)
    await session.flush()
    assert await engine.expire_stale(session) >= 1
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.EXPIRED


async def test_parallel_workers_fulfill_exactly_once(engine) -> None:  # type: ignore[no-untyped-def]
    from paurubot.payments import engine as pay_engine

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await _shop_with_cards(s)
        cust = await factories.customer(s, shop)
        started = await pay_engine.start(s, cust, "card", 70_000)
        await pay_engine.attach_receipt(s, started.payment, file_id="f", text="t", image=None)
        await pay_engine.approve_card(s, started.payment.id, staff_id=None)  # type: ignore[arg-type]
        await s.commit()
        ids = (shop.id, cust.id, started.payment.id)

    async def worker() -> bool:
        async with maker() as s2:
            return await pay_engine.fulfill_one(s2, ids[2]) is not None

    results = await asyncio.gather(*(worker() for _ in range(6)))
    assert sum(results) == 1
    async with maker() as s3:
        assert await wallet_balance(s3, ids[0], ids[1]) == 70_000
        payment = await s3.get(Payment, ids[2])
        assert payment is not None and payment.fulfilled_at is not None
        assert isinstance(await s3.get(Customer, ids[1]), Customer)


async def test_zarinpal_callback_route(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app

    provider, _ = _zarinpal(ref_id=4242)
    shop = await _zarinpal_shop(session, monkeypatch, provider)
    shop.bot_username = "pauru_test_bot"
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "zarinpal", 60_000)
    await session.commit()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get(
            "/api/pay/zarinpal/callback",
            params={"pid": started.payment.id, "Authority": "A0001", "Status": "OK"},
        )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"start=pay_paid_{started.payment.id}")
    assert await wallet_balance(session, shop.id, cust.id) == 60_000
