"""Explicit custom checkout API. No raw reference/tenant/role/price authority in bodies."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from paurubot import kv
from paurubot.api.deps import DB, StaffContext, require
from paurubot.api.webapp_auth import WebApp
from paurubot.audit.log import record
from paurubot.db.models import Payment
from paurubot.payments import tonpay_custom_runtime as service
from paurubot.payments import tonpay_custom_service as mutations
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.tonpay_custom import RECEIPT_LIMIT

router = APIRouter(tags=["tonpay-custom"])
# Bounded per-process image/request admission; not financial truth or a distributed mutex.
_images = asyncio.Semaphore(2)
PREFIX = "/api/webapp/tonpay"


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount: int = Field(gt=0, le=10**12)
    intent_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ConsentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote_revision: int = Field(gt=0, le=10**12)
    payable_amount: int = Field(gt=0, le=10**18 - 1)


class MutationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    quote_revision: int = Field(gt=0, le=10**12)


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=10, max_length=200)


async def _body(request: Request, limit: int) -> bytes:
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(status_code=413, detail="body_too_large")
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="request_timeout") from None
    return bytes(data)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _decode(data: bytes | str, model: type[BaseModel]) -> Any:
    return model.model_validate(json.loads(data, object_pairs_hook=_unique))


async def _json(request: Request, model: type[BaseModel]) -> Any:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(status_code=415, detail="unsupported_media_type")
    try:
        return _decode(await _body(request, 4096), model)
    except (ValidationError, ValueError, UnicodeError):
        # Never echo rejected input (may contain images, credentials or arbitrary fields).
        raise HTTPException(status_code=422, detail="invalid_request") from None


def _error(exc: PaymentError) -> HTTPException:
    code = getattr(exc, "code", exc.key)
    status = 404 if code == "not_found" else 409
    if code in {"method_disabled", "creation_disabled", "credential_changed"}:
        status = 403
    return HTTPException(
        status_code=status,
        detail=code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _owner(ctx: WebApp) -> mutations.Owner:
    return mutations.Owner(ctx.shop.id, ctx.customer.id)


async def _admit(ctx: WebApp, response: Response, *, write: bool) -> mutations.Owner:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    owner = _owner(ctx)
    action = "write" if write else "read"
    user_ok, _ = await kv.hit(f"tonpay:{owner.shop_id}:{owner.customer_id}:{action}", 6 if write else 12, 60)
    shop_ok, _ = await kv.hit(f"tonpay:{owner.shop_id}:{action}", 30 if write else 60, 60)
    if not user_ok or not shop_ok:
        raise HTTPException(status_code=429, detail="rate_limited")
    # webapp_context upserts the authenticated principal. Release its write locks
    # before the domain's independent durable transactions; no business mutation here.
    await ctx.session.commit()
    return owner


@router.post(PREFIX + "/create")
async def create(request: Request, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=True)
    body = await _json(request, CreateBody)
    try:
        return await service.start(owner, body.amount, body.intent_key)
    except PaymentError as exc:
        raise _error(exc) from None


@router.post(PREFIX + "/resume")
async def resume(request: Request, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=False)
    body = await _json(request, CreateBody)
    try:
        return await service.resume(owner, body.amount, body.intent_key)
    except PaymentError as exc:
        raise _error(exc) from None


@router.get(PREFIX + "/payments")
async def recent(ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=False)
    try:
        return {"items": await service.recent(owner)}
    except PaymentError as exc:
        raise _error(exc) from None


@router.get(PREFIX + "/{payment_id}")
async def view(payment_id: int, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=False)
    try:
        return await service.view(owner, payment_id)
    except PaymentError as exc:
        raise _error(exc) from None


@router.post(PREFIX + "/{payment_id}/refresh")
async def refresh(payment_id: int, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=False)
    try:
        return await service.refresh(owner, payment_id)
    except PaymentError as exc:
        raise _error(exc) from None


@router.post(PREFIX + "/{payment_id}/consent")
async def consent(payment_id: int, request: Request, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=True)
    body = await _json(request, ConsentBody)
    try:
        await service.client_for(owner, payment_id)  # current key/config/owner, no remote call
        await mutations.consent(owner, payment_id, body.quote_revision, body.payable_amount)
        return await service.view(owner, payment_id)
    except PaymentError as exc:
        raise _error(exc) from None


@router.post(PREFIX + "/{payment_id}/change-card")
async def change_card(payment_id: int, request: Request, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=True)
    body = await _json(request, MutationBody)
    try:
        client = await service.client_for(owner, payment_id)
        result = await mutations.change_card(
            owner, payment_id, body.intent_key, body.quote_revision, client=client
        )
        return {"result": asdict(result), "payment": await service.view(owner, payment_id)}
    except PaymentError as exc:
        raise _error(exc) from None


@router.post(PREFIX + "/{payment_id}/receipt")
async def receipt(payment_id: int, request: Request, ctx: WebApp, response: Response) -> dict[str, Any]:
    owner = await _admit(ctx, response, write=True)
    # Control metadata is a small JSON header; image body is bounded while streaming,
    # not an unbounded Starlette multipart file parsed before authentication.
    try:
        body = _decode(request.headers.get("x-tonpay-operation", "")[:2048], MutationBody)
    except (ValidationError, ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="invalid_request") from None
    if request.headers.get("content-type", "").split(";")[0] not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail="unsupported_media_type")
    try:
        client = await service.client_for(owner, payment_id)
        # Deny uncertain effects before decoding. Completed same-key receipt replays
        # must still reach the domain fingerprint check, even when the fresh action is closed.
        current = await service.view(owner, payment_id)
        if current["uncertain"]:
            raise mutations.CustomStateError("mutation_uncertain")
        try:
            await asyncio.wait_for(_images.acquire(), timeout=0.05)
        except TimeoutError:
            raise HTTPException(status_code=429, detail="upload_busy") from None
        try:
            data = await _body(request, RECEIPT_LIMIT)
            result = await mutations.submit_receipt(
                owner, payment_id, body.intent_key, body.quote_revision, data, client=client
            )
        finally:
            _images.release()
        return {"result": asdict(result), "payment": await service.view(owner, payment_id)}
    except PaymentError as exc:
        raise _error(exc) from None


@router.post("/api/admin/payments/{payment_id}/tonpay-reconcile")
async def reconcile(
    payment_id: int,
    request: Request,
    ctx: Annotated[StaffContext, Depends(require("payments.review"))],
    db: DB,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await _json(request, ReconcileBody)
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    payment = await db.scalar(
        select(Payment).where(
            Payment.id == payment_id, Payment.shop_id == ctx.shop_id, Payment.provider == "tonpay_custom"
        )
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    allowed, _ = await kv.hit(f"tonpay:admin:{ctx.shop_id}:{ctx.staff.id}", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate_limited")
    owner = mutations.Owner(ctx.shop_id, payment.customer_id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="tonpay_custom.reconcile_requested",
        entity="payment",
        entity_id=payment_id,
        after={"reason": body.reason, "mode": "GET_only_no_repost"},
    )
    await db.commit()  # durable reason/actor before external GET; no manual override
    try:
        result = await service.refresh(owner, payment_id)
    except PaymentError as exc:
        raise _error(exc) from None
    # Never send card details to the diagnostic endpoint or expose operation hashes/tokens.
    result.pop("card", None)
    return result


@router.get("/api/pay/tonpay-custom", include_in_schema=False)
@router.post("/api/pay/tonpay-custom", include_in_schema=False)
async def callback_hint() -> Response:
    # Ignore all callback/query/body claims. Only an authenticated bounded GET can confirm.
    return Response(
        "received; verify in the authenticated checkout",
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
