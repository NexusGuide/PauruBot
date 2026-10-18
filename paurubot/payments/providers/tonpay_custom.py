"""Unregistered Tonpay custom API client; not a payment method or settlement authority.

Source-derived from Faoxima a96ccf23. Calls are single-attempt: a failed mutation
may already have reached the provider. The future service must durably record
intents, enforce owner/shop/state/cooldown and reconcile before retrying.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import warnings
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from paurubot.payments.base import PaymentError

RECEIPT_LIMIT = 10 * 1024 * 1024
PIXEL_LIMIT = 20_000_000
RESPONSE_LIMIT = 64 * 1024
BASE_URL = "https://tonpays.online/api/custom/v1"


class CustomAPIError(PaymentError):
    """Sanitized machine reason, never provider text or credential-bearing payload."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__("gateway_error")
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class CustomInvoice:
    invoice_id: str
    # Create/change/receipt may omit order_id in the reference contract. Only check()
    # requires it; an absent field is not silently replaced with claimed authority.
    order_id: str | None
    request_amount: int
    payable_amount: int  # includes provider fee, NOT the wallet credit amount
    status: str
    paid: bool
    card_number: str = field(default="", repr=False)
    card_name: str = field(default="", repr=False)
    receipt_received: bool = False
    change_card_exhausted: bool = False

    @property
    def fee(self) -> int:
        return self.payable_amount - self.request_amount


@dataclass(frozen=True, slots=True)
class ReceiptImage:
    content: bytes = field(repr=False)
    filename: str
    mime: str


def _positive(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CustomAPIError("invalid_amount")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,18}", text) or int(text) <= 0:
        raise CustomAPIError("invalid_amount")
    return int(text)


def _identity(value: Any, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{maximum}}}", value):
        raise CustomAPIError("invalid_identity")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise CustomAPIError("invalid_response")
    return value


def _label(value: Any, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CustomAPIError("invalid_response")
    return value


def _callback(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise CustomAPIError("invalid_callback")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (
            url.scheme != "https"
            or not host
            or url.username is not None
            or url.password is not None
            or url.port not in (None, 443)
            or url.fragment
            or "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
        ):
            raise CustomAPIError("invalid_callback")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise CustomAPIError("invalid_callback")
    except ValueError as exc:
        raise CustomAPIError("invalid_callback") from exc
    # Not a DNS-safety service. The future caller must supply its configured server
    # callback, NEVER a customer URL, and enforce deployment/public-origin policy.
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def receipt_image(content: bytes) -> ReceiptImage:
    """Bounded, fully decoded single-frame JPEG/PNG/WebP; MIME is not caller-supplied.

    No filesystem/URL input, original filename, or persistent receipt storage. This
    is file validation, not evidence of payment or proof that the image is a receipt.
    """
    if not isinstance(content, bytes) or not content or len(content) > RECEIPT_LIMIT:
        raise CustomAPIError("invalid_receipt")
    formats = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if (
                    image.format not in formats
                    or width <= 0
                    or height <= 0
                    or width * height > PIXEL_LIMIT
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise CustomAPIError("invalid_receipt")
                extension, mime = formats[image.format]
                image.verify()
            # verify() alone does not fully decode JPEG raster data.
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise CustomAPIError("invalid_receipt") from None
    return ReceiptImage(content, f"receipt.{extension}", mime)


def _invoice(
    data: dict[str, Any],
    order_id: str,
    amount: int,
    *,
    invoice_id: str | None = None,
    require_order: bool = False,
) -> CustomInvoice:
    reference = _identity(data.get("invoice_id"))
    if invoice_id is not None and reference != invoice_id:
        raise CustomAPIError("identity_mismatch")
    remote_order = data.get("order_id")
    if remote_order is not None and (isinstance(remote_order, bool) or str(remote_order) != order_id):
        raise CustomAPIError("identity_mismatch")
    if require_order and remote_order is None:
        raise CustomAPIError("identity_mismatch")
    requested = _positive(data.get("request_amount"))
    payable = _positive(data.get("final_amount"))
    if requested != amount or payable < requested:
        raise CustomAPIError("amount_mismatch")
    if data.get("currency", "IRT") != "IRT":
        raise CustomAPIError("currency_mismatch")
    return CustomInvoice(
        reference,
        str(remote_order) if remote_order is not None else None,
        requested,
        payable,
        _label(data.get("status", ""), 40).strip().lower(),
        _boolean(data, "paid"),
        _label(data.get("card_number", ""), 40),
        _label(data.get("card_name", ""), 120),
        _boolean(data, "receipt_received"),
        _boolean(data, "change_card_exhausted"),
    )


class TonpayCustomClient:
    """Low-level contract only. No availability, callback handler or wallet mutation.

    Possession of a reference is NOT authorization. Future service entry points must
    load the caller-owned payment and its original snapshot, never accept arbitrary
    order/amount/reference from client input. HTTP calls do not enforce local policy.
    """

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or re.search(r"[\x00-\x1f\x7f]", api_key):
            raise CustomAPIError("missing_key")
        self._api_key = api_key
        self._transport = transport

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=5), transport=self._transport, follow_redirects=False
            ) as client:
                async with client.stream(
                    method,
                    f"{BASE_URL}/{path}",
                    headers={"X-API-Key": self._api_key, "Accept": "application/json"},
                    **kwargs,
                ) as response:
                    if response.status_code == 429:
                        hint = response.headers.get("Retry-After", "")
                        delay = min(int(hint), 3600) if re.fullmatch(r"[0-9]{1,8}", hint) else None
                        raise CustomAPIError("rate_limited", retry_after=delay)
                    if not 200 <= response.status_code < 300:
                        raise CustomAPIError("upstream_error")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > RESPONSE_LIMIT:
                            raise CustomAPIError("response_too_large")
                        raw.extend(chunk)
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
                raise CustomAPIError("invalid_response")
            return data
        except (httpx.HTTPError, ValueError, RecursionError):
            raise CustomAPIError("upstream_error") from None

    async def create(
        self, order_id: str, amount: int, buyer_chat_id: int, callback_url: str
    ) -> CustomInvoice:
        order_id = _identity(order_id, maximum=20)
        # No random/guessed buyer identifier. Future service derives this from its
        # authenticated Telegram principal, never a Mini App body or query field.
        if (
            isinstance(buyer_chat_id, bool)
            or not isinstance(buyer_chat_id, int)
            or not 0 < buyer_chat_id < 2**63
        ):
            raise CustomAPIError("invalid_buyer")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise CustomAPIError("invalid_amount")
        amount = _positive(amount)
        callback_url = _callback(callback_url)
        data = await self._request(
            "POST",
            "invoices/telegram/create",
            json={
                "order_id": order_id,
                "amount": amount,
                "buyer_chat_id": buyer_chat_id,
                "callback_url": callback_url,
            },
        )
        return _invoice(data, order_id, amount)

    async def check(self, invoice_id: str, order_id: str, amount: int) -> CustomInvoice:
        invoice_id = _identity(invoice_id)
        order_id = _identity(order_id, maximum=20)
        amount = _positive(amount)
        data = await self._request("GET", f"invoices/check/{invoice_id}")
        return _invoice(data, order_id, amount, invoice_id=invoice_id, require_order=True)

    async def change_card(self, invoice_id: str, order_id: str, amount: int) -> CustomInvoice:
        invoice_id = _identity(invoice_id)
        order_id = _identity(order_id, maximum=20)
        amount = _positive(amount)
        data = await self._request("POST", f"invoices/{invoice_id}/change-card", json={})
        invoice = _invoice(data, order_id, amount, invoice_id=invoice_id)
        if not invoice.card_number:
            raise CustomAPIError("invalid_response")
        # Fee changes must be surfaced for fresh user consent by the future service.
        return invoice

    async def submit_receipt(
        self, invoice_id: str, order_id: str, amount: int, content: bytes
    ) -> CustomInvoice:
        invoice_id = _identity(invoice_id)
        order_id = _identity(order_id, maximum=20)
        amount = _positive(amount)
        image = await asyncio.to_thread(receipt_image, content)
        data = await self._request(
            "POST",
            f"invoices/{invoice_id}/receipt",
            files={
                "file": (image.filename, image.content, image.mime),
            },
        )
        invoice = _invoice(data, order_id, amount, invoice_id=invoice_id)
        if not invoice.receipt_received:
            raise CustomAPIError("receipt_not_received")
        # This is an upload acknowledgement, NOT a Verification or settlement.
        return invoice
