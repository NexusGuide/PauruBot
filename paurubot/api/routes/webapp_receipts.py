"""Customer receipt correction contract. No settlement or staff actions are exposed."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from paurubot.api.webapp_auth import WebApp
from paurubot.db.models import Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments.base import PaymentError
from paurubot.worker import queue

router = APIRouter()
MAX_RECEIPT = 8 * 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def revision(payment: Payment) -> str:
    # This is an optimistic concurrency token, never an authorization credential.
    value = [
        payment.id,
        payment.status.value,
        payment.provider,
        payment.amount,
        payment.currency,
        _iso(payment.expires_at),
        payment.receipt_file_id,
        payment.receipt_text,
        payment.meta,
        _iso(payment.fulfilled_at),
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def receipt_view(payment: Payment) -> dict[str, Any]:
    meta = payment.meta
    invalid = not isinstance(meta, (dict, type(None)))
    requests = meta.get("fix_requests", []) if isinstance(meta, dict) else []
    correction = None
    if not isinstance(requests, list):
        invalid = True
    elif requests:
        last = requests[-1]
        if not isinstance(last, dict) or last.get("kind") not in ("amount", "card"):
            invalid = True
        else:
            note, at = last.get("note"), last.get("at")
            if not (note is None or isinstance(note, str)) or not isinstance(at, str):
                invalid = True
            else:
                try:
                    parsed = datetime.fromisoformat(at)
                    if parsed.tzinfo is None:
                        invalid = True
                except ValueError:
                    invalid = True
                if not invalid:
                    correction = {"kind": last["kind"], "note": note, "requested_at": at}
    submitted = bool(payment.receipt_file_id or payment.receipt_text)
    expired = payment.expires_at is not None and payment.expires_at <= datetime.now(UTC)
    status = payment.status.value
    if payment.provider != "card":
        phase = "not_card"
    elif status == "paid":
        phase = "paid"
    elif status in ("failed", "expired", "refunded", "disputed", "voided"):
        phase = "closed"
    elif status == "verifying":
        phase = "under_review"
    elif invalid or status != "pending" or submitted:
        phase = "unknown"
    elif expired:
        phase = "deadline_passed"
    else:
        phase = "correction_required" if correction else "awaiting_receipt"
    return {
        "id": payment.id,
        "provider": payment.provider,
        "status": status,
        "amount": payment.amount,
        "currency": payment.currency,
        "order_id": payment.order_id,
        "fulfilled": payment.fulfilled_at is not None,
        "expires_at": _iso(payment.expires_at),
        "revision": revision(payment),
        "correction": correction,
        "receipt_submitted": submitted,
        "phase": phase,
        "can_upload": phase in ("awaiting_receipt", "correction_required"),
    }


async def _owned(ctx: WebApp, payment_id: int, *, lock: bool = False) -> Payment:
    statement = select(Payment).where(
        Payment.id == payment_id, Payment.customer_id == ctx.customer.id, Payment.shop_id == ctx.shop.id
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    payment = await ctx.session.scalar(statement)
    if payment is None:
        raise HTTPException(404, "payment_not_found")
    return payment


@router.get("/payments/{payment_id}/receipt-state")
async def state(payment_id: int, ctx: WebApp) -> dict[str, Any]:
    return receipt_view(await _owned(ctx, payment_id))


@router.get("/receipts")
async def inbox(ctx: WebApp, before: Annotated[int | None, Query(gt=0)] = None) -> dict[str, Any]:
    statement = select(Payment).where(
        Payment.customer_id == ctx.customer.id, Payment.shop_id == ctx.shop.id, Payment.provider == "card"
    )
    if before is not None:
        statement = statement.where(Payment.id < before)
    rows = list(await ctx.session.scalars(statement.order_by(Payment.id.desc()).limit(21)))
    return {
        "items": [receipt_view(row) for row in rows[:20]],
        "next_before": rows[19].id if len(rows) > 20 else None,
    }


def validate_image(data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or getattr(image, "is_animated", False):
                raise ValueError("unsupported image")
            if image.width * image.height > 20_000_000:
                raise ValueError("image too large")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "receipt_invalid_image") from exc


async def _store(ctx: WebApp, data: bytes) -> str | None:
    # Lazy import avoids a router cycle and reuses the existing Telegram storage path.
    from paurubot.api.routes.webapp import _store_receipt

    return await _store_receipt(ctx, data)


@router.post("/payments/{payment_id}/receipt-upload")
async def upload(
    payment_id: int,
    ctx: WebApp,
    expected_revision: Annotated[str, Form(pattern=r"^[a-f0-9]{64}$")],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    return await submit_upload(payment_id, ctx, file, expected_revision=expected_revision)


async def submit_upload(
    payment_id: int,
    ctx: WebApp,
    file: UploadFile,
    *,
    expected_revision: str | None,
    allow_legacy_initial: bool = False,
) -> dict[str, Any]:
    """Both upload URLs share ownership, locking, validation, settlement and commit rules."""
    payment = await _owned(ctx, payment_id, lock=True)
    view = receipt_view(payment)
    if expected_revision is None:
        # An old client cannot acknowledge a new operator instruction it has not read.
        if not allow_legacy_initial or view["correction"] is not None:
            raise HTTPException(409, "receipt_revision_required")
    elif revision(payment) != expected_revision:
        raise HTTPException(409, "receipt_state_changed")
    if not view["can_upload"]:
        raise HTTPException(409, "payment_not_waiting")
    before_revision = revision(payment)
    data = await file.read(MAX_RECEIPT + 1)
    if len(data) > MAX_RECEIPT:
        raise HTTPException(413, "receipt_too_large")
    validate_image(data)
    file_id = await _store(ctx, data)
    if not file_id:
        raise HTTPException(502, "receipt_storage_failed")
    if not receipt_view(payment)["can_upload"]:
        raise HTTPException(409, "payment_not_waiting")
    try:
        duplicate = await engine.attach_receipt(ctx.session, payment, file_id=file_id, text=None, image=data)
    except PaymentError as exc:
        raise HTTPException(409, exc.key) from exc
    # The engine can approve trusted initial receipts or block duplicates. Read back its real state.
    await ctx.session.refresh(payment)
    if payment.status == PaymentStatus.VERIFYING:
        # fix_requests is capped at ten. A count-only key loses subsequent reviews.
        version = hashlib.sha256(f"{before_revision}:{revision(payment)}".encode()).hexdigest()
        await queue.enqueue(
            ctx.session,
            "notify",
            "receipt_review",
            {"payment_id": payment.id, "duplicate_of": duplicate.id if duplicate else None},
            shop_id=ctx.shop.id,
            dedupe_key=f"payment:{payment.id}:review:{version}",
        )
    result = receipt_view(payment)
    await ctx.session.commit()
    return result
