"""Hosted crypto invoices: NowPayments and Plisio (PAY-10).

Both are priced in US dollars converted from Toman with the USDT rate. Their callbacks are
signed (HMAC-SHA512 and HMAC-SHA1); a callback is accepted only with a valid signature, the
invoice and order we created, the exact dollar amount and a final "paid" status.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import ROUND_UP, Decimal
from typing import Any

import httpx

from paurubot.config import get_settings
from paurubot.db.models import Payment
from paurubot.payments import fx
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider, Verification

log = logging.getLogger(__name__)

NOWPAYMENTS = "https://api.nowpayments.io/v1"
PLISIO = "https://api.plisio.net/api/v1"


def usd_cents(toman: int, usdt_rate: int) -> int:
    dollars = (Decimal(toman) / Decimal(usdt_rate)).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return int(dollars * 100)


def dollars(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _same_amount(value: Any, cents: int | None) -> bool:
    try:
        return cents is not None and int((Decimal(str(value)) * 100).to_integral_value()) == cents
    except (ArithmeticError, ValueError):
        return False


async def invoice_cents(payment: Payment, cfg: dict[str, Any]) -> int:
    """Freeze the same conversion used by creation before persisting a hosted attempt."""
    try:
        return usd_cents(payment.amount, await fx.rate(cfg, "usdt"))
    except fx.RateUnavailable as exc:
        raise PaymentError("rate_unavailable") from exc


class _Invoice(PaymentProvider):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), transport=self._transport, follow_redirects=False
        )

    async def _cents(self, payment: Payment, cfg: dict[str, Any]) -> int:
        attempt = (payment.meta or {}).get("hosted_attempt_v1")
        if (
            isinstance(attempt, dict)
            and attempt.get("state") == "uncertain"
            and payment.provider_currency == "USD"
            and payment.provider_amount is not None
        ):
            return payment.provider_amount
        return await invoice_cents(payment, cfg)


class NowPaymentsProvider(_Invoice):
    name = "nowpayments"

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        if not (
            cfg.get("nowpayments_enabled")
            and cfg.get("nowpayments_api_key")
            and cfg.get("nowpayments_ipn_secret")
        ):
            return False
        if not get_settings().public_url:
            return False
        try:
            await fx.rate(cfg, "usdt")
        except fx.RateUnavailable:
            return False
        return True

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        cents = await self._cents(payment, cfg)
        body = {
            "price_amount": float(dollars(cents)),
            "price_currency": "usd",
            "order_id": str(payment.id),
            "order_description": f"Payment #{payment.id}",
            "ipn_callback_url": f"{get_settings().public_url}/api/pay/nowpayments/ipn",
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{NOWPAYMENTS}/invoice", json=body, headers={"x-api-key": cfg["nowpayments_api_key"]}
                )
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("nowpayments invoice failed: %s", exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        if resp.status_code >= 300 or not data.get("invoice_url") or not data.get("id"):
            log.warning("nowpayments rejected payment %s", payment.id)
            raise PaymentError("gateway_error")
        return Checkout(
            redirect_url=str(data["invoice_url"]),
            provider_ref=str(data["id"]),
            provider_amount=cents,
            provider_currency="USD",
        )

    @staticmethod
    def signature(raw: bytes, secret: str) -> str:
        ordered = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
        return hmac.new(secret.encode(), ordered.encode(), hashlib.sha512).hexdigest()

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        raw: bytes = params.get("raw") or b""
        given = str(params.get("signature") or "")
        secret = str(cfg.get("nowpayments_ipn_secret") or "")
        if not secret:
            # Anyone can compute an HMAC with an empty key: an unset secret never verifies anything.
            return Verification(False, detail="waiting: no ipn secret")
        try:
            expected = self.signature(raw, secret)
            data = json.loads(raw)
        except ValueError:
            return Verification(False, detail="bad body")
        if not given or not hmac.compare_digest(given, expected):
            return Verification(False, detail="bad signature")
        if str(data.get("order_id")) != str(payment.id) or str(data.get("invoice_id")) != str(
            payment.provider_ref
        ):
            return Verification(False, detail="invoice mismatch")
        if str(data.get("price_currency", "")).lower() != "usd" or not _same_amount(
            data.get("price_amount"), payment.provider_amount
        ):
            return Verification(False, detail="amount mismatch")
        status = str(data.get("payment_status") or "")
        if status in {"failed", "expired", "refunded"}:
            return Verification(False, detail=f"canceled: {status}")
        if status != "finished":
            return Verification(False, detail=f"waiting: {status}")
        pid = data.get("payment_id")
        if pid is None or not str(pid).strip():
            return Verification(False, detail="missing payment_id")
        return Verification(True, provider_tx_id=f"np:{pid}", amount=payment.amount, currency="IRT")


class PlisioProvider(_Invoice):
    name = "plisio"

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        if not (cfg.get("plisio_enabled") and cfg.get("plisio_api_key") and get_settings().public_url):
            return False
        try:
            await fx.rate(cfg, "usdt")
        except fx.RateUnavailable:
            return False
        return True

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        cents = await self._cents(payment, cfg)
        params = {
            "source_currency": "USD",
            "source_amount": dollars(cents),
            "order_number": str(payment.id),
            "order_name": f"Payment {payment.id}",
            "callback_url": f"{get_settings().public_url}/api/pay/plisio/callback?json=true",
            "api_key": cfg["plisio_api_key"],
        }
        try:
            async with self._client() as client:
                resp = await client.get(f"{PLISIO}/invoices/new", params=params)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("plisio invoice failed: %s", exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        data = body.get("data") or {}
        if body.get("status") != "success" or not data.get("invoice_url") or not data.get("txn_id"):
            log.warning("plisio rejected payment %s", payment.id)
            raise PaymentError("gateway_error")
        return Checkout(
            redirect_url=str(data["invoice_url"]),
            provider_ref=str(data["txn_id"]),
            provider_amount=cents,
            provider_currency="USD",
        )

    @staticmethod
    def signature(data: dict[str, Any], secret: str) -> str:
        ordered = {k: v for k, v in data.items() if k != "verify_hash"}
        text = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
        return hmac.new(secret.encode(), text.encode(), hashlib.sha1).hexdigest()

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        secret = str(cfg.get("plisio_api_key") or "")
        if not secret:
            return Verification(False, detail="waiting: no api key")
        try:
            data = json.loads(params.get("raw") or b"")
        except ValueError:
            return Verification(False, detail="bad body")
        if not isinstance(data, dict):
            return Verification(False, detail="bad body")
        given = str(data.get("verify_hash") or "")
        expected = self.signature(data, secret)
        if not given or not hmac.compare_digest(given, expected):
            return Verification(False, detail="bad signature")
        if str(data.get("order_number")) != str(payment.id) or str(data.get("txn_id")) != str(
            payment.provider_ref
        ):
            return Verification(False, detail="invoice mismatch")
        if str(data.get("source_currency", "")).upper() != "USD" or not _same_amount(
            data.get("source_amount"), payment.provider_amount
        ):
            return Verification(False, detail="amount mismatch")
        status = str(data.get("status") or "")
        if status in {"expired", "cancelled", "error"}:
            return Verification(False, detail=f"canceled: {status}")
        if status != "completed":
            return Verification(False, detail=f"waiting: {status}")
        return Verification(
            True, provider_tx_id=f"plisio:{data.get('txn_id')}", amount=payment.amount, currency="IRT"
        )
