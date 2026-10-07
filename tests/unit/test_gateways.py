"""Iranian redirect gateways (PAY-08, PAY-09): create, verify, signatures, amounts, late money."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import engine
from paurubot.payments.providers import gateways as gw
from tests import factories

TRX_WALLET = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"


class FakeGateways:
    """One mock transport for every gateway host, with knobs per test."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, Any]] = []
        self.verify_ok = True
        self.paid_amount: int | None = None  # in the gateway's own unit; None = what was asked
        self.state = "PAID"
        self.asked: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else None
        path, host = request.url.path, request.url.host
        self.seen.append((request.method, f"{host}{path}", body))
        ok = self.verify_ok
        if host == "panel.aqayepardakht.ir":
            if path.endswith("/create"):
                self.asked["aqayepardakht"] = body["amount"]
                return httpx.Response(200, json={"status": "success", "transid": "T-1"})
            return httpx.Response(
                200, json={"status": "success" if ok else "error", "code": "1" if ok else "0"}
            )
        if host == "tetra98.com":
            if path.endswith("create_order"):
                return httpx.Response(
                    200,
                    json={"status": "100", "Authority": "AU-1", "payment_url_bot": "https://t.me/tetra?s=1"},
                )
            return httpx.Response(200, json={"status": 100 if ok else 0})
        if host == "variza.ir":
            return httpx.Response(200, json={"slug": "vz-1", "pay_url": "https://variza.ir/p/vz-1"})
        if host == "aban.example.com":
            if path.endswith("/create"):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "authority": "abn_abcdef12",
                        "payment_link": "https://aban.example.com/pay/x",
                    },
                )
            amount = self.paid_amount if self.paid_amount is not None else body["amount"] * 10
            return httpx.Response(200, json={"success": ok, "order_id": body["order_id"], "amount": amount})
        if host == "blupal.net":
            if path.endswith("/create"):
                self.asked["blupal"] = body["amount"]
                return httpx.Response(
                    200, json={"success": True, "invoice_id": 991, "payment_link": "https://blupal.net/i/991"}
                )
            amount = self.paid_amount if self.paid_amount is not None else self.asked["blupal"]
            return httpx.Response(
                200, json={"success": True, "invoice_id": 991, "status": self.state, "amount": amount}
            )
        if host == "bot.tronado.cloud":
            if path.endswith("GetPriceToToman"):
                return httpx.Response(200, json={"TronPriceToman": 20000})
            self.asked["tronado"] = body["TronAmount"]
            return httpx.Response(
                200,
                json={
                    "IsSuccessful": True,
                    "Data": {"Token": "TK-1", "FullPaymentUrl": "https://t.me/tronado_robot/x"},
                },
            )
        if host == "cubevps.ir":
            if path.endswith("create-order.php"):
                self.asked["cubepay"] = body["price_amount"]
                return httpx.Response(
                    200, json={"success": True, "pay_page_url": "https://cubevps.ir/p/1", "authority": "CB-1"}
                )
            amount = self.paid_amount if self.paid_amount is not None else self.asked["cubepay"] * 10
            order = self.seen[0][2]["order_id"]
            return httpx.Response(200, json={"success": ok, "order_id": order, "amount": amount})
        if host == "api.atlaspay.space":
            if request.method == "POST":
                self.asked["atlaspay"] = body["baseAmountToman"]
                return httpx.Response(
                    200,
                    json={
                        "orderId": "AT-1",
                        "customerStartLink": "https://t.me/atlas?start=AT-1",
                        "totalAmountToman": body["baseAmountToman"] + 1500,
                    },
                )
            return httpx.Response(
                200,
                json={"data": {"id": "AT-1", "status": self.state.lower(), "requiresManualDelivery": False}},
            )
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):  # type: ignore[no-untyped-def]
    server = FakeGateways()
    transport = httpx.MockTransport(server)
    for name, cls in (
        ("aqayepardakht", gw.AqayepardakhtProvider),
        ("tetra98", gw.Tetra98Provider),
        ("variza", gw.VarizaProvider),
        ("abangateway", gw.AbanGatewayProvider),
        ("blupal", gw.BlupalProvider),
        ("tronado", gw.TronadoProvider),
        ("cubepay", gw.CubePayProvider),
        ("atlaspay", gw.AtlasPayProvider),
    ):
        provider = cls(transport)
        monkeypatch.setitem(engine.PROVIDERS, name, provider)
        monkeypatch.setitem(gw.GATEWAYS, name, provider)
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    return server


CONFIG: dict[str, dict[str, Any]] = {
    "aqayepardakht": {"aqayepardakht_pin": "pin-1"},
    "tetra98": {"tetra98_api_key": "k"},
    "variza": {"variza_api_token": "tok", "variza_webhook_secret": "whsec"},
    "abangateway": {"abangateway_url": "https://aban.example.com/api", "abangateway_api_key": "k"},
    "blupal": {"blupal_api_key": "k"},
    "tronado": {"tronado_api_key": "k", "tronado_ipn_key": "ipn", "tronado_wallet": TRX_WALLET},
    "cubepay": {"cubepay_api_key": "cube-key", "cubepay_fee_percent": 10},
    "atlaspay": {"atlaspay_api_key": "k"},
}


async def _start(session, name: str, amount: int = 100_000, **extra: Any):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {f"{name}_enabled": True, **CONFIG[name], **extra})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, name, amount)
    return shop, cust, started


async def _credited(session, shop, cust, payment_id: int) -> int:  # type: ignore[no-untyped-def]
    await engine.fulfill_one(session, payment_id)
    return await wallet_balance(session, shop.id, cust.id)


def _request(**body: Any) -> dict[str, Any]:
    raw = json.dumps(body).encode()
    return {"source": "callback", "json": body, "raw": raw, "headers": {}, "query": {}, "form": {}}


async def test_disabled_without_keys_or_public_address(session, fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cfg = await shop_settings.load(session, shop.id)
    assert not await gw.GATEWAYS["aqayepardakht"].enabled({**cfg, "aqayepardakht_enabled": True})
    on = {
        **cfg,
        "aqayepardakht_enabled": True,
        "aqayepardakht_pin": "p",
        "atlaspay_enabled": True,
        "atlaspay_api_key": "k",
    }
    assert await gw.GATEWAYS["aqayepardakht"].enabled(on)
    monkeypatch.setattr(get_settings(), "public_url", "")
    assert not await gw.GATEWAYS["aqayepardakht"].enabled(on)
    assert await gw.GATEWAYS["atlaspay"].enabled(on)  # polled, needs no callback address


async def test_aqayepardakht_verifies_with_the_gateway(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "aqayepardakht")
    assert started.checkout.redirect_url == "https://panel.aqayepardakht.ir/startpay/T-1"
    create = fake.seen[0][2]
    assert create["callback"] == f"https://bot.example.com/api/pay/gw/aqayepardakht?pid={started.payment.id}"
    assert create["amount"] == 100_000
    assert not await engine.verify(session, started.payment.id, {"form": {"transid": "OTHER"}})
    fake.verify_ok = False
    assert not await engine.verify(session, started.payment.id, {"form": {"transid": "T-1"}})
    fake.verify_ok = True
    assert await engine.verify(session, started.payment.id, {"form": {"transid": "T-1"}})
    assert fake.seen[-1][2] == {"pin": "pin-1", "amount": 100_000, "transid": "T-1"}
    assert await _credited(session, shop, cust, started.payment.id) == 100_000


async def test_tetra98_sends_rial_and_an_unsigned_cancel_closes_nothing(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "tetra98")
    assert fake.seen[0][2]["Amount"] == "1000000"
    assert started.checkout.redirect_url == "https://t.me/tetra?s=1"
    # anyone can send status=0 for any payment id: it must not fail the payment
    assert not await engine.verify(session, started.payment.id, {"query": {"status": "0"}})
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.PENDING
    assert await engine.verify(session, started.payment.id, {"json": {"authority": "AU-1", "status": "100"}})


def _variza_hook(body: dict[str, Any], secret: str = "whsec") -> dict[str, Any]:
    params = _request(**body)
    sig = hmac.new(secret.encode(), params["raw"], hashlib.sha256).hexdigest()
    params["headers"] = {"x-webhook-signature": f"sha256={sig}"}
    return params


async def test_variza_only_trusts_the_signed_webhook(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "variza")
    pid = started.payment.id
    assert started.checkout.redirect_url == "https://variza.ir/p/vz-1"
    # the browser coming back proves nothing
    assert not await engine.verify(session, pid, {"query": {"pid": str(pid)}})
    paid = {"event": "payment.paid", "slug": "vz-1", "amount": 100_037}
    assert not await engine.verify(session, pid, _variza_hook(paid, secret="wrong"))
    assert not await engine.verify(session, pid, _variza_hook({**paid, "slug": "vz-2"}))
    assert await engine.verify(session, pid, _variza_hook(paid))
    assert await _credited(session, shop, cust, pid) == 100_000


async def test_variza_short_payment_is_disputed(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "variza")
    hook = _variza_hook({"event": "payment.paid", "slug": "vz-1", "amount": 90_000})
    assert not await engine.verify(session, started.payment.id, hook)
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.DISPUTED
    assert await wallet_balance(session, shop.id, cust.id) == 0


async def test_abangateway_checks_order_and_rial_amount(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "abangateway")
    assert fake.seen[0][1] == "aban.example.com/api/create"
    fake.paid_amount = 100_000  # Toman reported where Rial is expected: a tenth of the money
    assert not await engine.verify(session, started.payment.id, {})
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.DISPUTED
    fake.paid_amount = None
    _, cust2, again = await _start(session, "abangateway")
    assert await engine.verify(session, again.payment.id, {})
    assert fake.seen[-1][2] == {
        "authority": "abn_abcdef12",
        "order_id": str(again.payment.id),
        "amount": 100_000,
    }


async def test_abangateway_toman_mode(session, fake) -> None:  # type: ignore[no-untyped-def]
    fake.paid_amount = 100_000
    shop, cust, started = await _start(session, "abangateway", abangateway_rial=False)
    assert await engine.verify(session, started.payment.id, {})


async def test_blupal_is_polled_and_exact(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "blupal")
    assert fake.asked["blupal"] == 1_000_000
    fake.state = "PENDING"
    await session.execute(
        update(Payment)
        .where(Payment.id == started.payment.id)
        .values(created_at=utcnow() - timedelta(minutes=1))
    )
    await session.commit()
    assert await engine.poll_gateways(session) == 0
    fake.state = "PAID"
    assert await engine.poll_gateways(session) == 1
    assert await wallet_balance(session, shop.id, cust.id) == 100_000


async def test_blupal_webhook_route_finds_payment_by_invoice(session, fake) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app

    shop, cust, started = await _start(session, "blupal")
    await session.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        unknown = await client.post(
            "/api/pay/gw/blupal", json={"event": "payment.completed", "invoice_id": 5}
        )
        resp = await client.post("/api/pay/gw/blupal", json={"event": "payment.completed", "invoice_id": 991})
        nope = await client.post("/api/pay/gw/nothing", json={})
    assert unknown.status_code == 404 and nope.status_code == 404
    assert resp.status_code == 200 and resp.json() == {"status": "paid"}
    assert await wallet_balance(session, shop.id, cust.id) == 100_000


def _tronado_hook(payment_id: int, trx: float, status: int = 30, key: str = "ipn") -> dict[str, Any]:
    params = _request(
        PaymentId=str(payment_id),
        OrderStatusID=status,
        IsPaid=status == 30,
        TronAmount=trx,
        ActualTronAmount=trx,
    )
    params["headers"] = {"x-tronado-sig": hmac.new(key.encode(), params["raw"], hashlib.sha512).hexdigest()}
    return params


async def test_tronado_needs_its_signature_and_the_full_trx(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "tronado")
    pid = started.payment.id
    assert fake.asked["tronado"] == 5.0  # 100,000 Toman at 20,000 Toman per TRX
    assert started.payment.provider_amount == 5_000_000 and started.payment.provider_currency == "TRX"
    assert gw.TronadoProvider.locate(_tronado_hook(pid, 5.0)) == ("id", str(pid))
    assert not await engine.verify(session, pid, {**_tronado_hook(pid, 5.0), "headers": {}})
    assert not await engine.verify(session, pid, _tronado_hook(pid, 5.0, key="forged"))
    assert not await engine.verify(session, pid, _tronado_hook(pid, 5.0, status=20))
    assert await engine.verify(session, pid, _tronado_hook(pid, 5.0))
    assert await _credited(session, shop, cust, pid) == 100_000
    _, _, short = await _start(session, "tronado")
    assert not await engine.verify(session, short.payment.id, _tronado_hook(short.payment.id, 4.5))
    await session.refresh(short.payment)
    assert short.payment.status == PaymentStatus.DISPUTED


async def test_cubepay_card_and_crypto(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "cubepay")
    assert fake.asked["cubepay"] == 110_000  # 10% fee on top
    assert engine.amount_to_pay(started.payment) == 110_000
    assert await engine.verify(session, started.payment.id, {"query": {"authority": "CB-1"}})
    assert await _credited(session, shop, cust, started.payment.id) == 100_000

    _, _, crypto = await _start(session, "cubepay")
    pid = str(crypto.payment.id)

    def signed(status: str, amount: str, key: str = "cube-key") -> dict[str, Any]:
        sig = hmac.new(key.encode(), f"{pid}|{status}|{amount}".encode(), hashlib.sha256).hexdigest()
        return {
            "form": {"order_id": pid, "status": status, "amount": amount, "sig": sig, "payment_id": "cp9"}
        }

    assert not await engine.verify(session, crypto.payment.id, signed("paid", "110000", key="other"))
    assert not await engine.verify(session, crypto.payment.id, signed("pending", "110000"))
    assert await engine.verify(session, crypto.payment.id, signed("paid", "110000"))


async def test_cubepay_forged_authority_cannot_close_a_payment(session, fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "cubepay")
    original = fake.__call__

    def expired_for_strangers(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("verify-payment.php")
            and json.loads(request.content)["authority"] != "CB-1"
        ):
            return httpx.Response(410, json={"success": False})
        return original(request)

    monkeypatch.setattr(fake, "__call__", expired_for_strangers)
    gw.GATEWAYS["cubepay"]._transport = httpx.MockTransport(expired_for_strangers)
    assert await engine.verify(session, started.payment.id, {"query": {"authority": "ATTACKER"}})
    assert fake.seen[-1][2] == {"authority": "CB-1"}  # our own authority was checked, not theirs


async def test_unset_secret_never_verifies(session, fake) -> None:  # type: ignore[no-untyped-def]
    assert not gw.signed("", b"body", hmac.new(b"", b"body", hashlib.sha256).hexdigest())
    assert gw.signed("k", b"body", "sha256=" + hmac.new(b"k", b"body", hashlib.sha256).hexdigest())
    shop, cust, started = await _start(session, "variza")
    await shop_settings.set_many(session, shop.id, {"variza_webhook_secret": ""})
    shop_settings.invalidate(shop.id)
    hook = _variza_hook({"event": "payment.paid", "slug": "vz-1", "amount": 100_000}, secret="")
    assert not await engine.verify(session, started.payment.id, hook)


async def test_staff_can_credit_what_really_arrived(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "variza")
    hook = _variza_hook({"event": "payment.paid", "slug": "vz-1", "amount": 90_000})
    assert not await engine.verify(session, started.payment.id, hook)
    await session.refresh(started.payment)
    assert engine.awaiting_review(started.payment)
    assert engine.disputed_credit(started.payment) == 90_000
    assert await engine.approve_manual(session, started.payment.id, None)  # type: ignore[arg-type]
    assert await _credited(session, shop, cust, started.payment.id) == 90_000
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.PAID and started.payment.amount == 90_000
    assert not engine.awaiting_review(started.payment)


async def test_late_credit_has_a_limit(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "aqayepardakht")
    await session.execute(
        update(Payment)
        .where(Payment.id == started.payment.id)
        .values(status=PaymentStatus.EXPIRED, expires_at=utcnow() - engine.LATE_CREDIT - timedelta(hours=1))
    )
    calls = len(fake.seen)
    assert not await engine.verify(session, started.payment.id, {"form": {"transid": "T-1"}})
    assert len(fake.seen) == calls  # not even asked


async def test_unsigned_callbacks_are_rate_limited(session, fake) -> None:  # type: ignore[no-untyped-def]
    from paurubot import kv
    from paurubot.api.app import create_app

    shop, cust, started = await _start(session, "aqayepardakht")
    fake.verify_ok = False
    await session.commit()
    pid = started.payment.id
    await kv.reset(f"gw:aqayepardakht:{pid}")
    before = len(fake.seen)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        for _ in range(10):
            await client.get("/api/pay/gw/aqayepardakht", params={"pid": pid})
    assert len(fake.seen) - before == 6
    await kv.reset(f"gw:aqayepardakht:{pid}")


async def test_atlaspay_polls_and_shows_the_marked_up_amount(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "atlaspay")
    assert engine.amount_to_pay(started.payment) == 101_500
    fake.state = "waiting"
    assert not await engine.verify(session, started.payment.id, {"source": "poll"})
    fake.state = "confirmed"
    assert await engine.verify(session, started.payment.id, {"source": "poll"})
    assert await _credited(session, shop, cust, started.payment.id) == 100_000


async def test_money_confirmed_after_expiry_is_still_credited(session, fake) -> None:  # type: ignore[no-untyped-def]
    shop, cust, started = await _start(session, "aqayepardakht")
    await session.execute(
        update(Payment)
        .where(Payment.id == started.payment.id)
        .values(status=PaymentStatus.EXPIRED, expires_at=utcnow() - timedelta(minutes=5))
    )
    assert await engine.verify(session, started.payment.id, {"form": {"transid": "T-1"}})
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.PAID and started.payment.review_note == "paid after expiry"
    assert await _credited(session, shop, cust, started.payment.id) == 100_000


async def test_browser_return_redirects_to_the_bot(session, fake) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.app import create_app

    shop, cust, started = await _start(session, "variza")
    shop.bot_username = "pauru_test_bot"
    await session.commit()
    pid = started.payment.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        back = await client.get("/api/pay/gw/variza", params={"pid": pid}, headers={"accept": "text/html"})
        body = json.dumps({"event": "payment.paid", "slug": "vz-1", "amount": 100_000}).encode()
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        hook = await client.post(
            "/api/pay/gw/variza",
            content=body,
            headers={"content-type": "application/json", "x-webhook-signature": sig},
        )
    assert back.status_code == 303 and back.headers["location"].endswith(f"start=pay_wait_{pid}")
    assert hook.json() == {"status": "paid"}
    assert await wallet_balance(session, shop.id, cust.id) == 100_000


def test_settings_validation() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.system import _check_gateways

    values: dict[str, Any] = {
        "abangateway_url": "https://aban.example.com/api/",
        "tronado_wallet": TRX_WALLET,
    }
    _check_gateways(values)
    assert values["abangateway_url"] == "https://aban.example.com/api"
    for bad in (
        {"abangateway_url": "http://aban.example.com"},
        {"abangateway_url": "https://localhost/api"},
        {"tronado_wallet": "not-an-address"},
        {"cubepay_fee_percent": 150},
    ):
        with pytest.raises(HTTPException):
            _check_gateways(dict(bad))


def test_parse_body_handles_json_form_and_junk() -> None:
    assert gw.parse_body(b'{"a": 1}', "application/json") == ({"a": 1}, {})
    assert gw.parse_body(b"a=1&b=2", "application/x-www-form-urlencoded") == (None, {"a": "1", "b": "2"})
    assert gw.parse_body(b"{broken", "application/json") == (None, {})
    assert gw.parse_body(b"", "") == (None, {})
