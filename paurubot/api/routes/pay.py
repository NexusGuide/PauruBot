"""Payment gateway callbacks."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from paurubot import kv
from paurubot.api.body import BodyTooLarge, read_limited_body
from paurubot.api.deps import DB
from paurubot.db.models import Payment, PaymentStatus, Shop
from paurubot.payments import engine
from paurubot.payments.base import PaymentError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pay", tags=["payments"])


@router.get("/zarinpal/callback")
async def zarinpal_callback(
    db: DB,
    pid: int = Query(gt=0),
    authority: str = Query(alias="Authority", max_length=64),
    status: str = Query(alias="Status", max_length=8),
) -> Response:
    payment = await db.get(Payment, pid)
    if payment is None or payment.provider != "zarinpal":
        return Response("unknown payment", status_code=404, media_type="text/plain")
    try:
        ok = await engine.verify(db, pid, {"Authority": authority, "Status": status})
    except PaymentError:
        ok = False
    await db.commit()
    if ok:
        await engine.fulfill_one(db, pid)
    return await _return_page(db, payment, "paid" if ok else "failed")


async def _invoice_callback(
    db: DB, provider_name: str, payment_id: int, raw: bytes, signature: str
) -> Response:
    payment = await db.get(Payment, payment_id)
    if payment is None or payment.provider != provider_name:
        return Response("unknown payment", status_code=404, media_type="text/plain")
    try:
        ok = await engine.verify(db, payment_id, {"raw": raw, "signature": signature})
    except PaymentError:
        ok = False
    await db.commit()
    if ok:
        await engine.fulfill_one(db, payment_id)
    return Response("ok", media_type="text/plain")


MAX_CALLBACK = 64 * 1024


@router.post("/nowpayments/ipn")
async def nowpayments_ipn(request: Request, db: DB) -> Response:
    try:
        raw = await read_limited_body(request, MAX_CALLBACK)
    except BodyTooLarge:
        return Response("too large", status_code=413, media_type="text/plain")
    try:
        order_id = int(json.loads(raw).get("order_id"))
    except (ValueError, TypeError, AttributeError):
        return Response("bad request", status_code=400, media_type="text/plain")
    return await _invoice_callback(
        db, "nowpayments", order_id, raw, request.headers.get("x-nowpayments-sig", "")
    )


@router.post("/plisio/callback")
async def plisio_callback(request: Request, db: DB) -> Response:
    try:
        raw = await read_limited_body(request, MAX_CALLBACK)
    except BodyTooLarge:
        return Response("too large", status_code=413, media_type="text/plain")
    try:
        order_id = int(json.loads(raw).get("order_number"))
    except (ValueError, TypeError, AttributeError):
        return Response("bad request", status_code=400, media_type="text/plain")
    return await _invoice_callback(db, "plisio", order_id, raw, "")


GATEWAY_CHECKS_PER_MINUTE = 6


async def _return_page(db: DB, payment: Payment, outcome: str) -> Response:
    """Where the payer's browser lands: a guest's purchase page (CAT-08), else back in the bot."""
    from paurubot.db.models import Customer

    customer = await db.get(Customer, payment.customer_id)
    if customer is not None and customer.guest_token:
        # Never the token itself: anyone can hit a callback with a payment id. The buyer's own
        # browser carries the token in a cookie set when the purchase started.
        return RedirectResponse("/buy/return", status_code=303)
    return _back_to_bot(await db.get(Shop, payment.shop_id), outcome, payment.id)


def _back_to_bot(shop: Shop | None, outcome: str, pid: int) -> Response:
    if shop is not None and shop.bot_username:
        return RedirectResponse(
            f"https://t.me/{shop.bot_username}?start=pay_{outcome}_{pid}", status_code=303
        )
    return Response(f"payment {outcome}", media_type="text/plain")


# Preserve legacy dispatch (including the combined 405 Allow header) first;
# expose GET and POST separately in OpenAPI without duplicating handler logic.
@router.get("/gw/{name}", operation_id="gateway_callback_get")
@router.post("/gw/{name}", operation_id="gateway_callback_post")
@router.api_route("/gw/{name}", methods=["GET", "POST"], include_in_schema=False)
async def gateway_callback(name: str, request: Request, db: DB) -> Response:
    """One entry point for the redirect gateways (PAY-08, PAY-09): the payer's browser coming
    back and the gateway's own server pushes. Neither is trusted; the provider decides."""
    from paurubot.payments.providers.gateways import GATEWAYS, parse_body

    gateway = GATEWAYS.get(name)
    if gateway is None or engine.PROVIDERS.get(name) is not gateway:
        return Response("unknown gateway", status_code=404, media_type="text/plain")
    try:
        raw = await read_limited_body(request, MAX_CALLBACK)
    except BodyTooLarge:
        return Response("too large", status_code=413, media_type="text/plain")
    data, form = parse_body(raw, request.headers.get("content-type", ""))
    query = dict(request.query_params)
    params: dict[str, Any] = {
        "source": "callback",
        "query": query,
        "form": form,
        "json": data,
        "raw": raw,
        "headers": {k.lower(): v for k, v in request.headers.items()},
    }
    payment: Payment | None = None
    pid = str(query.get("pid") or "")
    hint = ("id", pid) if pid else gateway.locate(params)
    if hint is not None and hint[0] == "id" and hint[1].isdigit() and len(hint[1]) < 19:
        payment = await db.get(Payment, int(hint[1]))
    elif hint is not None and hint[0] == "ref" and len(hint[1]) <= 255:
        payment = await db.scalar(
            select(Payment).where(Payment.provider == name, Payment.provider_ref == hint[1]).limit(1)
        )
    if payment is None or payment.provider != name:
        return Response("unknown payment", status_code=404, media_type="text/plain")
    payment_id = payment.id
    allowed = True
    if gateway.throttled(params) and payment.status != PaymentStatus.PAID:
        # Each of these makes us call the gateway; a stranger replaying the URL can't make it a flood.
        allowed, _ = await kv.hit(f"gw:{name}:{payment_id}", GATEWAY_CHECKS_PER_MINUTE, 60)
    try:
        ok = await engine.verify(db, payment_id, params) if allowed else False
    except PaymentError:
        ok = False
    await db.commit()
    if ok:
        await engine.fulfill_one(db, payment_id)
    await db.refresh(payment)
    outcome = "paid" if ok else ("wait" if payment.status in engine.OPEN_STATUSES else "failed")
    if payment.status == PaymentStatus.DISPUTED:
        outcome = "wait"
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return await _return_page(db, payment, outcome)
    return JSONResponse({"status": {"paid": "paid", "wait": "waiting"}.get(outcome, "failed")})
