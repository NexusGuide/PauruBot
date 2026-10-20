"""Authenticated card registration, separate from payment receipt uploads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from paurubot import kv
from paurubot.api.webapp_auth import WebApp
from paurubot.core import cards
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import to_latin_digits
from paurubot.db.models import Customer, VerifiedCard
from paurubot.security.card_photo import PhotoError, decode_photo, sanitize_photo
from paurubot.worker import queue

router = APIRouter()
MAX_BODY = 4 * 1024 * 1024 + 1024
KEY = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")


def view(card: VerifiedCard) -> dict[str, Any]:
    return {"id": card.id, "masked": card.masked, "status": card.status}


def response(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def read_body(request: Request) -> tuple[str, str, bytes]:
    # Request instead of Form/Body: the WebApp dependency authenticates before reading any bytes.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "card_request_invalid")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "card_request_invalid")
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_BODY:
                    raise HTTPException(413, "card_photo_too_large")
                data.extend(chunk)
        body = json.loads(data, object_pairs_hook=_object)
        if not isinstance(body, dict) or set(body) != {"intent_key", "number", "image"}:
            raise ValueError("fields")
        key, number, image = body["intent_key"], body["number"], body["image"]
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("key")
        if not isinstance(number, str) or len(number) > 32:
            raise ValueError("number")
        number = to_latin_digits(number).replace(" ", "").replace("-", "")
        if not re.fullmatch(r"[0-9]{16}", number) or not cards.luhn_ok(number):
            raise HTTPException(422, "card_number_invalid")
        if not isinstance(image, str):
            raise ValueError("image")
        return key, number, decode_photo(image)
    except TimeoutError:
        raise HTTPException(408, "card_request_timeout") from None
    except PhotoError as exc:
        raise HTTPException(422, str(exc)) from None
    except (ValueError, UnicodeError, RecursionError):
        # Never echo PAN, image contents or Pydantic's input validation payload to the caller/logs.
        raise HTTPException(422, "card_request_invalid") from None


async def store_photo(ctx: WebApp, data: bytes) -> str:
    from aiogram.types import BufferedInputFile

    from paurubot.notifications import telegram

    try:
        async with asyncio.timeout(15):
            message = await telegram.send_photo(
                ctx.customer.telegram_id,
                BufferedInputFile(data, "card-verification.jpg"),
                shop_id=ctx.shop.id,
                disable_notification=True,
            )
        if message and message.photo:
            return str(message.photo[-1].file_id)
    except Exception:
        # Do not log the request, image, card number or Telegram exception carrying request data.
        pass
    raise HTTPException(503, "card_photo_unavailable")


async def policy(ctx: WebApp) -> dict[str, Any]:
    cfg = await shop_settings.load(ctx.session, ctx.shop.id)
    return {
        "enabled": bool(cfg["card_verify_enabled"]),
        "min_amount": int(cfg["card_verify_min_amount"] or 0),
        "trusted": ctx.customer.card_trust == "trusted",
        "blocked": ctx.customer.card_trust == "blocked",
    }


@router.get("/cards/verification")
async def card_verification(ctx: WebApp) -> JSONResponse:
    """Own masked card verification status and the current shop policy."""
    p = await policy(ctx)
    rows = list(
        await ctx.session.scalars(
            select(VerifiedCard)
            .where(VerifiedCard.shop_id == ctx.shop.id, VerifiedCard.customer_id == ctx.customer.id)
            .order_by(VerifiedCard.id.desc())
            .limit(20)
        )
    )
    pending = await ctx.session.scalar(
        select(VerifiedCard.id)
        .where(
            VerifiedCard.shop_id == ctx.shop.id,
            VerifiedCard.customer_id == ctx.customer.id,
            VerifiedCard.status == "pending",
        )
        .limit(1)
    )
    approved = await cards.has_verified(ctx.session, ctx.customer)
    return response(
        {
            **p,
            "approved": approved,
            "can_submit": p["enabled"]
            and not p["blocked"]
            and not p["trusted"]
            and pending is None
            and not approved,
            "items": [view(card) for card in rows],
        }
    )


@router.post("/cards/verification")
async def submit_card_verification(request: Request, ctx: WebApp) -> JSONResponse:
    """Register one card photograph for existing staff review; never start a payment."""
    allowed, _ = await kv.hit(f"webapp-card:{ctx.shop.id}:{ctx.customer.id}", 10, 60)
    if not allowed:
        raise HTTPException(429, "rate_limited")
    key, number, raw = await read_body(request)
    fingerprint = hashlib.sha256(
        f"{ctx.shop.id}:{ctx.customer.id}:{number}:{hashlib.sha256(raw).hexdigest()}".encode()
    ).hexdigest()
    customer = await ctx.session.scalar(
        select(Customer)
        .where(Customer.id == ctx.customer.id, Customer.shop_id == ctx.shop.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if customer is None or customer.is_banned or customer.card_trust == "blocked":
        raise HTTPException(403, "card_verification_blocked")
    existing = await ctx.session.scalar(
        select(VerifiedCard).where(
            VerifiedCard.shop_id == ctx.shop.id,
            VerifiedCard.customer_id == customer.id,
            VerifiedCard.client_intent_key == key,
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "card_verification_conflict")
        return response({"card": view(existing)})
    p = await policy(ctx)
    if not p["enabled"] or p["trusted"]:
        raise HTTPException(409, "card_verification_unavailable")
    if await cards.has_verified(ctx.session, customer):
        raise HTTPException(409, "card_already_verified")
    pending = await ctx.session.scalar(
        select(VerifiedCard.id)
        .where(
            VerifiedCard.shop_id == ctx.shop.id,
            VerifiedCard.customer_id == customer.id,
            VerifiedCard.status == "pending",
        )
        .limit(1)
    )
    if pending is not None:
        raise HTTPException(409, "card_verify_pending")
    try:
        clean = await asyncio.to_thread(sanitize_photo, raw)
    except PhotoError as exc:
        raise HTTPException(422, str(exc)) from None
    file_id = await store_photo(ctx, clean)
    try:
        card, other = await cards.register(ctx.session, customer, number, file_id)
    except cards.CardError as exc:
        raise HTTPException(409, exc.key) from None
    card.client_intent_key = key
    card.request_fingerprint = fingerprint
    await ctx.session.flush()
    await queue.enqueue(
        ctx.session,
        "notify",
        "card_verify",
        {"card_id": card.id, "other_customer_id": other.id if other else None},
        shop_id=ctx.shop.id,
        dedupe_key=f"card:{card.id}:review",
    )
    result = view(card)
    await ctx.session.commit()
    return response({"card": result})
