"""Bank deposit SMS from the shop's forwarder app (REF-02); see paurubot.payments.bank_sms."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request

from paurubot import kv
from paurubot.api.body import BodyTooLarge, read_limited_body
from paurubot.api.deps import DB, request_ip
from paurubot.core import settings as shop_settings
from paurubot.db.models import Shop
from paurubot.payments import bank_sms
from paurubot.security.tokens import constant_equals
from paurubot.worker import queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sms", tags=["webhooks"])

MAX_BODY = 16 * 1024
PER_MINUTE = 120  # a busy shop gets a few deposit SMS a minute, never hundreds
REFUSED_PER_MINUTE = 30
REPORTED = {"unmatched", "ambiguous", "approved"}


def _fields(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type or text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {key: values[0] for key, values in parse_qs(text, keep_blank_values=True).items() if values}


def _key(request: Request, item: bank_sms.Envelope) -> str | None:
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return item.key  # forwarders that can only put it in the body


@router.post("/card/{shop_id}")
async def card_sms(shop_id: int, request: Request, db: DB) -> dict[str, str]:
    """A bank deposit SMS from the shop's forwarder app (REF-02); the key is the shop's SMS secret."""
    try:
        raw = await read_limited_body(request, MAX_BODY)
    except BodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="too_large") from exc
    item = bank_sms.envelope(_fields(raw, (request.headers.get("content-type") or "").lower()))
    shop = await db.get(Shop, shop_id)
    cfg = await shop_settings.load(db, shop_id) if shop is not None else {}
    secret = str(cfg.get("card_sms_secret") or "")
    # Unknown shop, feature off, weak or wrong key: one answer, so none of them can be told apart.
    if (
        item is None
        or not cfg.get("card_sms_enabled")
        or len(secret) < bank_sms.SECRET_MIN
        or not constant_equals(_key(request, item), secret)
    ):
        allowed, _ = await kv.hit(f"card-sms-refused:{request_ip(request)}", REFUSED_PER_MINUTE, 60)
        raise HTTPException(status_code=401 if allowed else 429, detail="unauthorized")
    # Counted per shop only after the key matched, so strangers cannot use up a shop's quota.
    allowed, _ = await kv.hit(f"card-sms:{shop_id}", PER_MINUTE, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate_limited")
    if item.is_test:
        return {"status": "test"}
    result = await bank_sms.match(db, shop_id, item)
    if result.outcome in REPORTED:
        await queue.enqueue(
            db,
            "notify",
            "card_sms",
            {
                "outcome": result.outcome,
                "payment_id": result.payment_id,
                "amount_rial": result.deposit.amount_rial if result.deposit else None,
                "candidates": result.candidates,
                "text": bank_sms.normalize(item.text)[:500] if result.outcome != "approved" else None,
            },
            shop_id=shop_id,
            dedupe_key=f"{bank_sms.fingerprint(shop_id, item)}:report",
        )
    await db.commit()
    return {"status": result.outcome}
