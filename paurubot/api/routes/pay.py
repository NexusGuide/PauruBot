"""Payment gateway callbacks."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from paurubot.api.deps import DB
from paurubot.db.models import Payment, Shop
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
    shop = await db.get(Shop, payment.shop_id)
    outcome = "paid" if ok else "failed"
    if shop is not None and shop.bot_username:
        return RedirectResponse(
            f"https://t.me/{shop.bot_username}?start=pay_{outcome}_{pid}", status_code=303
        )
    return Response(f"payment {outcome}", media_type="text/plain")


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
    raw = await request.body()
    if len(raw) > MAX_CALLBACK:
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
    raw = await request.body()
    if len(raw) > MAX_CALLBACK:
        return Response("too large", status_code=413, media_type="text/plain")
    try:
        order_id = int(json.loads(raw).get("order_number"))
    except (ValueError, TypeError, AttributeError):
        return Response("bad request", status_code=400, media_type="text/plain")
    return await _invoice_callback(db, "plisio", order_id, raw, "")
