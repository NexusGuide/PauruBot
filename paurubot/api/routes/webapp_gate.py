"""The phone and terms gate of the Mini App, and the way through it (SEC-F11).

Every other Mini App route answers `phone_required` or `terms_required` until the customer has
done what the shop asks, the same order and the same rules as the bot. These two routes stay
open. The screen reads what is missing. Terms are accepted here, for the exact version shown.
The phone is shared with Telegram's own contact request: Telegram sends it to the bot, which
checks that it is the customer's own number and its country code, like a contact typed in chat.
"""

from __future__ import annotations

import html
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from paurubot.api.webapp_auth import WebApp, account_gate
from paurubot.i18n import texts_for

router = APIRouter()

_TAG = re.compile(r"<[^>]{0,500}>")


def plain(text: str) -> str:
    """Telegram HTML to plain text for the Mini App: tags dropped, entities decoded."""
    return html.unescape(_TAG.sub("", text.replace("<br>", "\n"))).strip()


class TermsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, le=2**31 - 1, strict=True)


async def _view(ctx: Any) -> dict[str, Any]:
    cfg, customer = ctx.cfg, ctx.customer
    need = await account_gate(ctx.session, ctx.shop, customer, cfg)
    view: dict[str, Any] = {"requirement": need, "bot_username": ctx.shop.bot_username}
    if need == "terms_required":
        texts = await texts_for(ctx.session, ctx.shop.id, customer.locale or ctx.shop.default_locale)
        view["terms"] = {"version": int(cfg["terms_version"]), "text": plain(texts("terms_text"))}
    return view


@router.get("/gate")
async def gate(ctx: WebApp) -> dict[str, Any]:
    """What the customer must still do, phone then terms, before the rest of the Mini App answers (SEC-F11)."""
    return await _view(ctx)


@router.post("/gate/terms")
async def accept_terms(body: TermsBody, ctx: WebApp) -> dict[str, Any]:
    """Accept the terms version that was shown; a newer version answers 409 terms_changed."""
    if await account_gate(ctx.session, ctx.shop, ctx.customer, ctx.cfg) == "phone_required":
        raise HTTPException(status_code=409, detail="phone_required")  # the bot's order: phone first
    if not ctx.cfg["terms_required"]:
        return await _view(ctx)
    if body.version != int(ctx.cfg["terms_version"]):
        # Accept only what was shown: terms changed while the screen was open.
        raise HTTPException(status_code=409, detail="terms_changed")
    ctx.customer.terms_accepted_version = body.version
    await ctx.session.commit()
    return await _view(ctx)
