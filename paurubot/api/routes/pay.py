"""Payment gateway callbacks."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
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
