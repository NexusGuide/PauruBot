"""Mini App authentication: every request carries Telegram initData (SEC-01)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.api.deps import DB
from paurubot.config import get_settings
from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.core.shops import root_shop
from paurubot.db.models import Customer, Shop
from paurubot.i18n import LOCALES
from paurubot.security.tokens import InitDataError, validate_init_data

HEADER = "x-telegram-init-data"
RATE_LIMIT = 120
RATE_WINDOW = 60


@dataclass(slots=True)
class WebAppContext:
    session: AsyncSession
    shop: Shop
    customer: Customer
    cfg: dict[str, Any]


async def webapp_context(request: Request, db: DB) -> WebAppContext:
    settings = get_settings()
    raw = request.headers.get(HEADER, "")
    try:
        user = validate_init_data(
            raw, settings.bot_token.get_secret_value(), max_age=settings.webapp_auth_max_age
        )
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail="invalid_init_data") from exc
    allowed, _ = await kv.hit(f"webapp:{user.id}", RATE_LIMIT, RATE_WINDOW)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate_limited")
    shop = await root_shop(db)
    if shop is None:
        raise HTTPException(status_code=503, detail="not_set_up")
    cfg = await shop_settings.load(db, shop.id)
    locale = (user.language_code or "").split("-")[0]
    customer, _ = await customers.upsert_from_telegram(
        db,
        shop.id,
        user.id,
        username=user.username,
        first_name=user.first_name,
        locale=locale if locale in LOCALES else shop.default_locale,
    )
    if customer.is_banned:
        raise HTTPException(status_code=403, detail="banned")
    if not cfg["bot_enabled"]:
        raise HTTPException(status_code=503, detail="maintenance")
    return WebAppContext(db, shop, customer, cfg)


WebApp = Annotated[WebAppContext, Depends(webapp_context)]
