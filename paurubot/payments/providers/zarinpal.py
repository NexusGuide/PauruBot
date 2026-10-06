"""Zarinpal v4 gateway (PAY-07). Amounts are sent in Toman (currency IRT)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from paurubot.config import get_settings
from paurubot.db.models import Payment
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider, Verification

log = logging.getLogger(__name__)

LIVE = "https://payment.zarinpal.com"
SANDBOX = "https://sandbox.zarinpal.com"


class ZarinpalProvider(PaymentProvider):
    name = "zarinpal"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _host(self, cfg: dict[str, Any]) -> str:
        return SANDBOX if cfg.get("zarinpal_sandbox") else LIVE

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), transport=self._transport, follow_redirects=False
        )

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        return bool(
            cfg.get("zarinpal_enabled") and cfg.get("zarinpal_merchant") and get_settings().public_url
        )

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        callback = f"{get_settings().public_url}/api/pay/zarinpal/callback?pid={payment.id}"
        body = {
            "merchant_id": cfg["zarinpal_merchant"],
            "amount": payment.amount,
            "currency": "IRT",
            "callback_url": callback,
            "description": f"Payment #{payment.id}",
            "metadata": {"order_id": str(payment.id)},
        }
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._host(cfg)}/pg/v4/payment/request.json", json=body)
            data = resp.json().get("data") or {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("zarinpal request failed: %s", exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        authority = data.get("authority") if isinstance(data, dict) else None
        if not authority or data.get("code") != 100:
            log.warning("zarinpal rejected payment %s", payment.id)
            raise PaymentError("gateway_error")
        return Checkout(
            redirect_url=f"{self._host(cfg)}/pg/StartPay/{authority}",
            provider_ref=str(authority),
            provider_amount=payment.amount,
            provider_currency="IRT",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        authority = str(params.get("Authority") or "")
        if not authority or authority != payment.provider_ref:
            return Verification(False, detail="authority mismatch")
        if str(params.get("Status", "")).upper() != "OK":
            return Verification(False, detail="canceled by payer")
        body = {"merchant_id": cfg["zarinpal_merchant"], "amount": payment.amount, "authority": authority}
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._host(cfg)}/pg/v4/payment/verify.json", json=body)
            data = resp.json().get("data") or {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("zarinpal verify failed: %s", exc.__class__.__name__)
            raise PaymentError("gateway_error") from exc
        if not isinstance(data, dict) or data.get("code") not in (100, 101):
            return Verification(
                False, detail=f"verify code {data.get('code') if isinstance(data, dict) else '?'}"
            )
        ref_id = data.get("ref_id")
        if not ref_id:
            return Verification(False, detail="missing ref_id")
        return Verification(
            True,
            provider_tx_id=str(ref_id),
            amount=payment.amount,
            currency="IRT",
            raw={"card_pan": data.get("card_pan"), "fee": data.get("fee")},
        )
