"""Authenticated customer wallet transfer adapters; no client-selected shop or balance."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from paurubot.api.webapp_auth import WebApp
from paurubot.core import settings as shop_settings
from paurubot.core import transfer_quotes, transfers
from paurubot.ledger.accounts import wallet_balance

router = APIRouter()


class QuoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    recipient: str = Field(min_length=1, max_length=64, pattern=r"^(?:[0-9]{1,19}|@[A-Za-z0-9_]{1,32})$")
    amount: int = Field(gt=0, le=transfers.MAX_AMOUNT, strict=True)

    @field_validator("recipient")
    @classmethod
    def recipient_range(cls, value: str) -> str:
        if not value.startswith("@") and not 0 < int(value) <= 2**63 - 1:
            raise ValueError("recipient Telegram ID out of range")
        return value


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=64, max_length=2048)


def _error(exc: transfers.TransferError) -> HTTPException:
    status = 409 if exc.key in {"transfer_quote_expired", "transfer_quote_invalid"} else 400
    return HTTPException(status_code=status, detail=exc.key)


@router.get("/wallet/transfer-policy")
async def policy(ctx: WebApp, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    cfg = await shop_settings.load(ctx.session, ctx.shop.id, fresh=True)
    return {
        "enabled": bool(cfg["wallet_transfer_enabled"]),
        "minimum": max(1, int(cfg["wallet_transfer_min"] or 0)),
        "maximum": transfers.MAX_AMOUNT,
        "daily_maximum": int(cfg["wallet_transfer_daily_max"] or 0),
        "currency": ctx.shop.currency,
    }


@router.post("/wallet/transfers/quote")
async def quote(body: QuoteBody, ctx: WebApp, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await transfer_quotes.quote(
            ctx.session, ctx.customer, body.recipient, body.amount, ctx.shop.currency
        )
    except transfers.TransferError as exc:
        raise _error(exc) from exc


@router.post("/wallet/transfers/confirm")
async def confirm(body: ConfirmBody, ctx: WebApp, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await transfer_quotes.confirm(ctx.session, ctx.customer, body.token, ctx.shop.currency)
    except transfers.TransferError as exc:
        raise _error(exc) from exc
    # Commit before acknowledging. A lost response can only replay this quote's durable journal.
    await ctx.session.commit()
    return {
        **result,
        "balance": await wallet_balance(ctx.session, ctx.shop.id, ctx.customer.id),
    }
