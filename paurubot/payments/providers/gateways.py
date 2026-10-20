"""Iranian payment gateways (PAY-08, PAY-09).

Every gateway here is settled by asking the gateway itself, never by trusting what reaches the
callback: the callback only tells us *which* payment to look at. The exceptions are gateways
without a verify API (Variza, Tronado, CubePay's crypto path), whose callbacks are accepted only
with a valid HMAC over the exact bytes they sent. Amounts are compared with what we asked for,
so a short payment ends up DISPUTED for staff instead of being credited.

All callbacks arrive at `/api/pay/gw/<name>`; `params` passed to `verify` carries the parsed
request (`query`, `form`, `json`, `raw`, `headers`) or `{"source": "poll"}` from the poller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import ssl
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from paurubot.config import get_settings
from paurubot.db.models import Payment
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider, Verification
from paurubot.security import ssrf
from paurubot.security.http import ValidatedTransport

log = logging.getLogger(__name__)

SUN = 1_000_000


def field(params: dict[str, Any], *names: str) -> str:
    """First non-empty value for any of `names` across the JSON body, form and query string."""
    for source in ("json", "form", "query"):
        data = params.get(source)
        if not isinstance(data, dict):
            continue
        for name in names:
            value = data.get(name)
            if value not in (None, ""):
                return str(value)
    return ""


def _body(data: Any) -> dict[str, Any]:
    """Some gateways wrap the useful part in "data"/"Data"; flatten one level."""
    if not isinstance(data, dict):
        return {}
    for key in ("data", "Data"):
        inner = data.get(key)
        if isinstance(inner, dict):
            return {**data, **inner}
    return data


def _int(value: Any) -> int | None:
    try:
        return int(Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true", "ok", "success"}


def signed(secret: str, message: bytes, given: str, digest: Any = hashlib.sha256) -> bool:
    """Constant-time HMAC check; an unset secret never verifies anything."""
    given = given.strip().lower().removeprefix("sha256=")
    if not secret or not given:
        return False
    expected = hmac.new(secret.encode(), message, digest).hexdigest()
    return hmac.compare_digest(expected, given)


def settle(payment: Payment, tx: str, received: int | None, wanted: int | None) -> Verification:
    """Paid in full credits the payment; a short payment reports its share, which `mark_paid`
    sees as an amount mismatch and parks as DISPUTED for staff."""
    if received is None or not wanted:
        return Verification(False, detail="amount missing")
    if received >= wanted:
        return Verification(True, provider_tx_id=tx, amount=payment.amount)
    share = payment.amount * max(0, received) // wanted
    return Verification(True, provider_tx_id=tx, amount=share, detail="underpaid")


class Gateway(PaymentProvider):
    """Shared HTTP plumbing and settings for redirect gateways."""

    late_ok = True  # a payment verified with the gateway after our deadline is still credited
    asks_gateway = True  # verify calls the gateway, so unsigned callbacks are rate limited
    needs_callback = True
    polls = False
    required: tuple[str, ...] = ()

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), transport=self._transport, follow_redirects=False
        )

    def _request_client(self, url: str) -> httpx.AsyncClient:
        """Select HTTP plumbing without storing per-shop state on shared providers."""
        return self._client()

    @staticmethod
    def public_url() -> str:
        return str(get_settings().public_url or "").rstrip("/")

    def callback_url(self, payment: Payment | None = None) -> str:
        base = f"{self.public_url()}/api/pay/gw/{self.name}"
        return f"{base}?pid={payment.id}" if payment is not None else base

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        if not cfg.get(f"{self.name}_enabled") or not all(cfg.get(key) for key in self.required):
            return False
        return bool(self.public_url()) or not self.needs_callback

    async def call(self, method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        """(HTTP status, flattened JSON body); network and parse errors become `gateway_error`."""
        try:
            async with self._request_client(url) as client:
                resp = await client.request(method, url, **kwargs)
            data = resp.json() if resp.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s %s failed: %s", self.name, method, exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        if resp.status_code >= 300:
            log.info("%s answered %s", self.name, resp.status_code)
        return resp.status_code, _body(data)

    @staticmethod
    def locate(params: dict[str, Any]) -> tuple[str, str] | None:
        """How to find the payment when the callback URL carries no `pid`: ("id"|"ref", value)."""
        return None

    def throttled(self, params: dict[str, Any]) -> bool:
        """Whether this callback makes us call the gateway (and so counts against a rate limit)."""
        return self.asks_gateway

    @staticmethod
    def _rejected(name: str) -> PaymentError:
        log.warning("%s refused to open a payment", name)
        return PaymentError("gateway_error")


# --- Aqayepardakht -------------------------------------------------------------------


class AqayepardakhtProvider(Gateway):
    name = "aqayepardakht"
    required = ("aqayepardakht_pin",)
    host = "https://panel.aqayepardakht.ir"

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        _, data = await self.call(
            "POST",
            f"{self.host}/api/v2/create",
            json={
                "pin": cfg["aqayepardakht_pin"],
                "amount": payment.amount,
                "callback": self.callback_url(payment),
                "invoice_id": str(payment.id),
            },
        )
        transid = str(data.get("transid") or "")
        if data.get("status") != "success" or not transid:
            raise self._rejected(self.name)
        return Checkout(
            redirect_url=f"{self.host}/startpay/{transid}",
            provider_ref=transid,
            provider_amount=payment.amount,
            provider_currency="IRT",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        sent = field(params, "transid")
        if sent and sent != payment.provider_ref:
            return Verification(False, detail="reference mismatch")
        _, data = await self.call(
            "POST",
            f"{self.host}/api/v2/verify",
            json={"pin": cfg["aqayepardakht_pin"], "amount": payment.amount, "transid": payment.provider_ref},
        )
        if str(data.get("code")) != "1":
            return Verification(False, detail=f"verify code {data.get('code')}")
        return Verification(True, provider_tx_id=f"aqp:{payment.provider_ref}", amount=payment.amount)


# --- Tetra98 (IranPay) ------------------------------------------------------------------


class Tetra98Provider(Gateway):
    name = "tetra98"
    required = ("tetra98_api_key",)
    host = "https://tetra98.com"

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        _, data = await self.call(
            "POST",
            f"{self.host}/api/create_order",
            json={
                "ApiKey": cfg["tetra98_api_key"],
                "Hash_id": str(payment.id),
                "Amount": str(payment.amount * 10),  # Rial
                "CallbackURL": self.callback_url(payment),
            },
        )
        authority = str(data.get("Authority") or data.get("authority") or "")
        link = str(data.get("payment_url_bot") or data.get("payment_url_web") or "")
        if str(data.get("status")) != "100" or not authority or not link.startswith("https://"):
            raise self._rejected(self.name)
        return Checkout(
            redirect_url=link,
            provider_ref=authority,
            provider_amount=payment.amount * 10,
            provider_currency="IRR",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        sent = field(params, "authority", "Authority")
        if sent and sent != payment.provider_ref:
            return Verification(False, detail="reference mismatch")
        # The callback's own status is unsigned: it only tells us not to bother asking yet.
        status = field(params, "status")
        if status and status != "100":
            return Verification(False, detail=f"waiting: status {status}")
        _, data = await self.call(
            "POST",
            f"{self.host}/api/verify",
            json={
                "ApiKey": cfg["tetra98_api_key"],
                "authority": payment.provider_ref,
                "hashid": str(payment.id),
            },
        )
        if str(data.get("status")) != "100":
            return Verification(False, detail=f"verify status {data.get('status')}")
        return Verification(True, provider_tx_id=f"tetra:{payment.provider_ref}", amount=payment.amount)


# --- Variza ------------------------------------------------------------------------------


class VarizaProvider(Gateway):
    """No verify API: only the signed `payment.paid` webhook settles a payment."""

    name = "variza"
    asks_gateway = False
    required = ("variza_api_token", "variza_webhook_secret")
    host = "https://variza.ir"

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        _, data = await self.call(
            "POST",
            f"{self.host}/api/v1/pay",
            headers={"Authorization": f"Bearer {cfg['variza_api_token']}", "Accept": "application/json"},
            json={
                "amount": payment.amount,
                "return_url": self.callback_url(payment),
                "title": f"Payment #{payment.id}",
                "expires_in": "1h",
            },
        )
        slug, link = str(data.get("slug") or ""), str(data.get("pay_url") or "")
        if not slug or not link.startswith("https://"):
            raise self._rejected(self.name)
        return Checkout(
            redirect_url=link, provider_ref=slug, provider_amount=payment.amount, provider_currency="IRT"
        )

    @staticmethod
    def locate(params: dict[str, Any]) -> tuple[str, str] | None:
        slug = field(params, "slug")
        return ("ref", slug) if slug else None

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        raw: bytes = params.get("raw") or b""
        given = str((params.get("headers") or {}).get("x-webhook-signature") or "")
        if not given:
            return Verification(False, detail="waiting: no webhook yet")
        if not signed(str(cfg.get("variza_webhook_secret") or ""), raw, given):
            return Verification(False, detail="bad signature")
        data = params.get("json") or {}
        if data.get("event") != "payment.paid":
            return Verification(False, detail=f"waiting: event {data.get('event')}")
        if str(data.get("slug") or "") != payment.provider_ref:
            return Verification(False, detail="reference mismatch")
        return settle(
            payment, f"variza:{payment.provider_ref}", _int(data.get("amount")), payment.provider_amount
        )


# --- AbanGateway -------------------------------------------------------------------------

ABAN_AUTHORITY = re.compile(r"^abn_[A-Za-z0-9_]{6,64}$")


class AbanGatewayProvider(Gateway):
    name = "abangateway"
    required = ("abangateway_url", "abangateway_api_key")

    @staticmethod
    def base(cfg: dict[str, Any]) -> str:
        url = str(cfg.get("abangateway_url") or "").strip().rstrip("/")
        if not url.startswith("https://"):
            raise PaymentError("gateway_error")
        return url

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {cfg['abangateway_api_key']}", "Accept": "application/json"}

    def _request_client(self, url: str) -> httpx.AsyncClient:
        if self._transport is not None:
            return super()._request_client(url)  # explicit, trusted provider-test seam
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5),
            transport=ValidatedTransport(url, context=ssl.create_default_context(), allow_private=False),
            trust_env=False,
            follow_redirects=False,
        )

    async def _safe_base(self, cfg: dict[str, Any]) -> str:
        url = self.base(cfg)
        try:
            httpx.URL(url)  # reject control characters and malformed HTTP URL syntax
            ssrf.check_static(url)
            # This is an API path prefix, not a query/fragment or credential carrier.
            parts = urlsplit(url)
            if parts.query or parts.fragment:
                raise ssrf.UnsafeUrl("gateway base must be a plain API path")
        except (ValueError, httpx.InvalidURL) as exc:
            log.warning("abangateway address refused: %s", exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        # DNS resolution and the socket handoff happen together in ValidatedTransport.
        # Never inherit the panel-private-host override for a payment gateway.
        return url

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        base = await self._safe_base(cfg)
        _, data = await self.call(
            "POST",
            f"{base}/create",
            headers=self._headers(cfg),
            json={
                "amount": payment.amount,
                "order_id": str(payment.id),
                "callback_url": self.callback_url(payment),
            },
        )
        authority, link = str(data.get("authority") or ""), str(data.get("payment_link") or "")
        if not _truthy(data.get("success")) or not ABAN_AUTHORITY.match(authority):
            raise self._rejected(self.name)
        if not link.startswith("https://"):
            raise self._rejected(self.name)
        unit = 10 if cfg.get("abangateway_rial", True) else 1
        return Checkout(
            redirect_url=link,
            provider_ref=authority,
            provider_amount=payment.amount * unit,
            provider_currency="IRR" if unit == 10 else "IRT",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        base = await self._safe_base(cfg)
        status, data = await self.call(
            "POST",
            f"{base}/verify",
            headers=self._headers(cfg),
            json={"authority": payment.provider_ref, "order_id": str(payment.id), "amount": payment.amount},
        )
        if status != 200 or not _truthy(data.get("success")):
            return Verification(False, detail=f"waiting: verify {status}")
        if str(data.get("order_id")) != str(payment.id):
            return Verification(False, detail="reference mismatch")
        tx = f"aban:{payment.provider_ref}"
        return settle(payment, tx, _int(data.get("amount")), payment.provider_amount)


# --- BluPal ------------------------------------------------------------------------------


class BlupalProvider(Gateway):
    """The webhook is set in the BluPal dashboard; open invoices are also polled."""

    name = "blupal"
    required = ("blupal_api_key",)
    needs_callback = False
    polls = True
    host = "https://blupal.net/api/v1"

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"X-API-Key": str(cfg["blupal_api_key"]), "Accept": "application/json"}

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        rial = payment.amount * 10
        _, data = await self.call(
            "POST", f"{self.host}/invoices/create", headers=self._headers(cfg), json={"amount": rial}
        )
        invoice, link = str(data.get("invoice_id") or ""), str(data.get("payment_link") or "")
        if not _truthy(data.get("success")) or not invoice or not link.startswith("https://"):
            raise self._rejected(self.name)
        return Checkout(
            redirect_url=link, provider_ref=invoice, provider_amount=rial, provider_currency="IRR"
        )

    @staticmethod
    def locate(params: dict[str, Any]) -> tuple[str, str] | None:
        invoice = field(params, "invoice_id")
        return ("ref", invoice) if invoice else None

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        status, data = await self.call(
            "GET", f"{self.host}/invoices/{payment.provider_ref}", headers=self._headers(cfg)
        )
        if status != 200 or not _truthy(data.get("success")):
            return Verification(False, detail=f"waiting: verify {status}")
        if str(data.get("invoice_id")) != str(payment.provider_ref):
            return Verification(False, detail="reference mismatch")
        state = str(data.get("status") or "").upper()
        if state in {"EXPIRED", "CANCELED", "CANCELLED"}:
            return Verification(False, detail=f"canceled: {state.lower()}")
        if state != "PAID":
            return Verification(False, detail=f"waiting: {state.lower()}")
        tx = f"blupal:{payment.provider_ref}"
        received = _int(data.get("amount"))
        if received != payment.provider_amount:
            # BluPal invoices are for an exact amount; anything else is for staff to look at.
            share = payment.amount * (received or 0) // max(1, payment.provider_amount or 1)
            return Verification(True, provider_tx_id=tx, amount=share, detail="amount differs")
        return Verification(True, provider_tx_id=tx, amount=payment.amount)


# --- Tronado -----------------------------------------------------------------------------


class TronadoProvider(Gateway):
    """TRX via Tronado; settled only by its HMAC-SHA512-signed callback."""

    name = "tronado"
    asks_gateway = False
    required = ("tronado_api_key", "tronado_ipn_key", "tronado_wallet")
    host = "https://bot.tronado.cloud"

    async def trx_price(self) -> Decimal:
        _, data = await self.call("POST", f"{self.host}/Tron/GetPriceToToman", json=[])
        try:
            price = Decimal(str(data.get("TronPriceToman")))
        except ArithmeticError as exc:
            raise PaymentError("rate_unavailable") from exc
        if not price.is_finite() or price <= 0:
            raise PaymentError("rate_unavailable")
        return price

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        price = await self.trx_price()
        trx = (Decimal(payment.amount) / price).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        wage = max(0, min(100, int(cfg.get("tronado_wage_percent") or 0)))
        _, data = await self.call(
            "POST",
            f"{self.host}/api/v5/GetOrderToken",
            params={"wageFromBusinessPercentage": wage},
            headers={"x-api-key": str(cfg["tronado_api_key"])},
            json={
                "PaymentID": str(payment.id),
                "WalletAddress": str(cfg["tronado_wallet"]),
                "TronAmount": float(trx),
                "CallbackUrl": self.callback_url(payment),
            },
        )
        token = str(data.get("Token") or "")
        if not token or data.get("IsSuccessful") is False:
            raise self._rejected(self.name)
        link = str(data.get("FullPaymentUrl") or "")
        if not link.startswith("https://"):
            link = f"{{https://t.me/tronado_robot/customerpayment?startapp={token}}}"
        return Checkout(
            redirect_url=link,
            provider_ref=token,
            provider_amount=int(trx * SUN),
            provider_currency="TRX",
            instructions={"trx": str(trx)},
        )

    @staticmethod
    def locate(params: dict[str, Any]) -> tuple[str, str] | None:
        pid = field(params, "PaymentId", "PaymentID")
        return ("id", pid) if pid.isdigit() else None

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        raw: bytes = params.get("raw") or b""
        given = str((params.get("headers") or {}).get("x-tronado-sig") or "")
        if not given:
            return Verification(False, detail="waiting: no signed callback")
        if not signed(str(cfg.get("tronado_ipn_key") or ""), raw, given, hashlib.sha512):
            return Verification(False, detail="bad signature")
        data = params.get("json") or {}
        if str(data.get("PaymentId") or data.get("PaymentID") or "") != str(payment.id):
            return Verification(False, detail="reference mismatch")
        state = _int(data.get("OrderStatusID"))
        if state in (40, 200):
            return Verification(False, detail=f"canceled: status {state}")
        if state != 30 and not _truthy(data.get("IsPaid")):
            return Verification(False, detail=f"waiting: status {state}")
        tx = f"tronado:{payment.provider_ref}"
        value = data.get("ActualTronAmount")
        if value is None:
            value = data.get("TronAmount")
        try:
            received = int(Decimal(str(value)) * SUN)
        except (ArithmeticError, ValueError):
            return Verification(False, detail="amount missing")
        wanted = payment.provider_amount or 0
        return settle(payment, tx, received + 1, wanted)  # one sun of rounding either way


# --- CubePay -----------------------------------------------------------------------------


class CubePayProvider(Gateway):
    """Card-to-card (verified with the gateway) or crypto (HMAC-signed callback)."""

    name = "cubepay"
    required = ("cubepay_api_key",)
    host = "https://cubevps.ir"

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {cfg['cubepay_api_key']}", "Accept": "application/json"}

    def throttled(self, params: dict[str, Any]) -> bool:
        return not field(params, "sig")

    @staticmethod
    def price(amount: int, cfg: dict[str, Any]) -> int:
        fee = max(0, min(100, int(cfg.get("cubepay_fee_percent") or 0)))
        return math.ceil(amount * (100 + fee) / 100)

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        total = self.price(payment.amount, cfg)
        _, data = await self.call(
            "POST",
            f"{self.host}/pay/create-order.php",
            headers=self._headers(cfg),
            json={
                "order_id": str(payment.id),
                "price_amount": total,
                "callback_url": self.callback_url(payment),
                "redirect_after_payment": False,
            },
        )
        link = str(data.get("pay_page_url") or data.get("payment_link") or "")
        if not _truthy(data.get("success")) or not link.startswith("https://"):
            raise self._rejected(self.name)
        return Checkout(
            redirect_url=link,
            provider_ref=str(data.get("authority") or "") or None,
            provider_amount=total,
            provider_currency="IRT",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        if field(params, "sig"):
            return self._crypto(payment, cfg, params)
        # Our own authority when the gateway gave one; a callback's is only a fallback, and a
        # verify failure for it must never close the payment.
        authority = str(payment.provider_ref or "") or field(params, "authority")
        trusted = bool(payment.provider_ref)
        if not authority:
            return Verification(False, detail="waiting: no authority")
        status, data = await self.call(
            "POST",
            f"{self.host}/smspay/api/verify-payment.php",
            headers=self._headers(cfg),
            json={"authority": authority},
        )
        if status == 410 and trusted:
            return Verification(False, detail="canceled: expired")
        if status not in (200, 409) or not _truthy(data.get("success")):
            return Verification(False, detail=f"waiting: verify {status}")
        if str(data.get("order_id")) != str(payment.id):
            return Verification(False, detail="reference mismatch")
        tx = f"cube:{authority}"
        return settle(payment, tx, _int(data.get("amount")), (payment.provider_amount or 0) * 10)  # Rial

    def _crypto(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        order, status = field(params, "order_id"), field(params, "status")
        amount = field(params, "amount", "amount_toman")
        message = f"{order}|{status}|{amount}".encode()
        if not signed(str(cfg.get("cubepay_api_key") or ""), message, field(params, "sig")):
            return Verification(False, detail="bad signature")
        if order != str(payment.id):
            return Verification(False, detail="reference mismatch")
        if status in {"expired", "failed"}:
            return Verification(False, detail=f"canceled: {status}")
        if status != "paid":
            return Verification(False, detail="waiting: not paid")
        tx = f"cube:crypto:{field(params, 'payment_id') or order}"
        return settle(payment, tx, _int(amount), payment.provider_amount)


# --- AtlasPay ----------------------------------------------------------------------------


class AtlasPayProvider(Gateway):
    """No callback at all: open orders are polled until they settle or close."""

    name = "atlaspay"
    required = ("atlaspay_api_key",)
    needs_callback = False
    polls = True
    host = "https://api.atlaspay.space/api/v1"

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"X-API-Key": str(cfg["atlaspay_api_key"]), "Accept": "application/json"}

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        _, data = await self.call(
            "POST",
            f"{self.host}/orders",
            headers=self._headers(cfg),
            json={"merchantOrderRef": str(payment.id), "baseAmountToman": payment.amount},
        )
        order, link = str(data.get("orderId") or ""), str(data.get("customerStartLink") or "")
        total = _int(data.get("totalAmountToman")) or payment.amount
        if not order or not link.startswith("https://") or total < payment.amount:
            raise self._rejected(self.name)
        return Checkout(redirect_url=link, provider_ref=order, provider_amount=total, provider_currency="IRT")

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        status, data = await self.call(
            "GET", f"{self.host}/orders/{payment.provider_ref}", headers=self._headers(cfg)
        )
        if status != 200:
            return Verification(False, detail=f"waiting: lookup {status}")
        if str(data.get("id") or data.get("orderId") or "") != str(payment.provider_ref):
            return Verification(False, detail="reference mismatch")
        state = str(data.get("status") or "").lower()
        if state in {"rejected", "expired", "cancelled", "canceled"}:
            return Verification(False, detail=f"canceled: {state}")
        if state not in {"confirmed", "settled"}:
            return Verification(False, detail=f"waiting: {state}")
        if _truthy(data.get("requiresManualDelivery")):
            # AtlasPay marks short payments this way; staff settle them from the AtlasPay side.
            return Verification(False, detail="waiting: underpaid, needs review")
        return Verification(True, provider_tx_id=f"atlas:{payment.provider_ref}", amount=payment.amount)


# --- Tonpay standard (tonpays.online; not the TON blockchain SDK) -----------------------


class TonpayProvider(Gateway):
    """Source-derived standard invoice contract; callbacks are refresh hints only.

    No custom receipt upload, automatic amount adjustment, or create retry. Credentials
    go only to the fixed API origin, never to a returned checkout URL or redirect.
    """

    name = "tonpay"
    required = ("tonpay_api_key",)
    polls = True
    host = "https://tonpays.online/api/v1/invoices"
    response_limit = 64 * 1024

    @staticmethod
    def _amount(value: Any) -> int | None:
        # Toman integers only: no bool, rounding, NaN/Infinity, or implausibly large value.
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return None
        text = str(value)
        if not re.fullmatch(r"[0-9]{1,18}", text):
            return None
        amount = int(text)
        return amount if amount > 0 else None

    @staticmethod
    def _reference(value: Any) -> str:
        # A single bounded, opaque path segment; do not allow dot-segment traversal.
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return ""
        value = str(value)
        return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""

    @staticmethod
    def _checkout_url(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
            return ""
        try:
            url = urlsplit(value)
            # Conservative source-derived allowlist, including Telegram's invoice bot.
            # Expanding it requires confirming the provider contract, not client input.
            if (
                url.scheme != "https"
                or url.hostname not in {"tonpays.online", "t.me"}
                or url.username is not None
                or url.password is not None
                or url.port not in (None, 443)
            ):
                return ""
        except ValueError:
            return ""
        return value

    async def _request(self, method: str, path: str, cfg: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        key = cfg.get("tonpay_api_key")
        if not isinstance(key, str) or not key.strip():
            raise PaymentError("gateway_error")
        try:
            async with self._client() as client:
                async with client.stream(
                    method,
                    f"{self.host}/{path}",
                    headers={"X-API-Key": key, "Accept": "application/json"},
                    **kwargs,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise PaymentError("gateway_error")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.response_limit:
                            raise PaymentError("gateway_error")
                        body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
                raise PaymentError("gateway_error")
            return data
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            # Do not log raw response bodies, headers, credentials, or checkout tokens.
            raise PaymentError("gateway_error") from exc

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        if self._amount(payment.amount) is None:
            raise PaymentError("gateway_error")
        data = await self._request(
            "POST",
            "create",
            cfg,
            json={
                "amount": payment.amount,
                "order_id": str(payment.id),
                "callback_url": self.callback_url(payment),
            },
        )
        reference = self._reference(data.get("invoice_id"))
        url = self._checkout_url(data.get("invoice_url"))
        if not reference or not url:
            raise PaymentError("gateway_error")
        return Checkout(
            redirect_url=url,
            provider_ref=reference,
            provider_amount=payment.amount,
            provider_currency="IRT",
        )

    @staticmethod
    def locate(params: dict[str, Any]) -> tuple[str, str] | None:
        order_id = field(params, "order_id")
        if order_id:
            return "id", order_id
        reference = field(params, "invoice_id")
        return ("ref", reference) if reference else None

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        reference = self._reference(payment.provider_ref)
        if not reference:
            return Verification(False, detail="waiting: missing invoice reference")
        for name, expected in (("order_id", str(payment.id)), ("invoice_id", reference)):
            sent = field(params, name)
            if sent and sent != expected:
                return Verification(False, detail="waiting: callback identity mismatch")
        # The header is optional in the reference implementation, never sufficient to pay.
        headers = params.get("headers") or {}
        given = headers.get("x-api-key") if isinstance(headers, dict) else None
        key = cfg.get("tonpay_api_key")
        if given is not None and (
            not isinstance(key, str)
            or not isinstance(given, str)
            or not hmac.compare_digest(key.encode(), given.encode())
        ):
            return Verification(False, detail="waiting: callback authentication mismatch")
        data = await self._request("GET", f"check/{quote(reference, safe='')}", cfg)
        remote_order = data.get("order_id")
        if isinstance(remote_order, bool) or str(remote_order) != str(payment.id):
            return Verification(False, detail="waiting: invoice order mismatch")
        if "invoice_id" in data and self._reference(data["invoice_id"]) != reference:
            return Verification(False, detail="waiting: invoice reference mismatch")
        if data.get("paid") is not True or data.get("status") != "completed":
            # No terminal transition based on callback or an unconfirmed invoice state.
            return Verification(False, detail="waiting: invoice not completed")
        if self._amount(data.get("request_amount")) != payment.amount:
            return Verification(False, detail="waiting: invoice request amount mismatch")
        actual = self._amount(data.get("final_amount"))
        if actual is None or data.get("currency", "IRT") != "IRT":
            return Verification(False, detail="waiting: invalid settlement amount or currency")
        # Both overpayment and underpayment go through engine.mark_paid's dispute path.
        return Verification(True, provider_tx_id=f"tonpay:{reference}", amount=actual, currency="IRT")


GATEWAYS: dict[str, Gateway] = {
    gateway.name: gateway
    for gateway in (
        AqayepardakhtProvider(),
        Tetra98Provider(),
        VarizaProvider(),
        AbanGatewayProvider(),
        BlupalProvider(),
        TronadoProvider(),
        CubePayProvider(),
        AtlasPayProvider(),
        TonpayProvider(),
    )
}


def parse_body(raw: bytes, content_type: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(json, form) from a callback body; anything unparseable is simply absent."""
    from urllib.parse import parse_qs

    text = raw.decode("utf-8", "replace") if raw else ""
    data: dict[str, Any] | None = None
    form: dict[str, Any] = {}
    if text.lstrip().startswith("{"):
        try:
            loaded = json.loads(text)
            data = loaded if isinstance(loaded, dict) else None
        except (ValueError, RecursionError):
            data = None
    elif "form" in content_type or "=" in text:
        form = {k: v[0] for k, v in parse_qs(text, max_num_fields=50).items() if v}
    return data, form
