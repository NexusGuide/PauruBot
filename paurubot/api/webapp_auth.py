"""Mini App authentication: every request carries Telegram initData (SEC-01).

The same Mini App is opened from the main bot and from every reseller bot (TEN-04). Telegram signs
initData with the token of the bot that opened it, so the signature itself tells us the shop: the
main bot's token means the root shop, a reseller bot's token means that reseller's shop. Nothing the
client sends decides the shop, so one shop's customers can never be signed into another shop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.api.deps import DB
from paurubot.config import get_settings
from paurubot.core import channels as channel_rules
from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.core.shops import root_shop
from paurubot.db.models import Customer, ForcedChannel, Shop, ShopStatus
from paurubot.i18n import LOCALES
from paurubot.notifications import telegram
from paurubot.security.tokens import InitDataError, WebAppUser, validate_init_data

HEADER = "x-telegram-init-data"
RATE_LIMIT = 120
RATE_WINDOW = 60
RESELLER_TOKENS_TTL = 60.0
JOIN_CACHE_TTL = 300
# The join screen calls these two; they must work before the customer has joined (GRW-07).
JOIN_PATHS = {"/api/webapp/join", "/api/webapp/join/check"}
# The phone/terms screen calls these; they must work before the customer passes that gate (SEC-F11).
GATE_PATHS = {"/api/webapp/gate", "/api/webapp/gate/terms", "/api/webapp/gate/phone"}

_reseller_tokens_cache: tuple[float, list[tuple[int, str]]] | None = None


@dataclass(slots=True)
class WebAppContext:
    session: AsyncSession
    shop: Shop
    customer: Customer
    cfg: dict[str, Any]


def match_init_data(
    raw: str, main_token: str, reseller_tokens: list[tuple[int, str]], *, max_age: int
) -> tuple[WebAppUser, int | None]:
    """Validate initData against the main bot, then each reseller bot.

    Returns the user and the reseller shop id whose bot signed it (None for the main bot).
    The main bot's error is raised when no token matches, so an expired main-bot session still
    reports "expired" rather than "bad signature".
    """
    try:
        return validate_init_data(raw, main_token, max_age=max_age), None
    except InitDataError as first:
        for shop_id, token in reseller_tokens:
            try:
                return validate_init_data(raw, token, max_age=max_age), shop_id
            except InitDataError:
                continue
        raise first


def forget_reseller_tokens() -> None:
    """Drop the cached reseller tokens (tests, and after a reseller bot changes)."""
    global _reseller_tokens_cache
    _reseller_tokens_cache = None


async def forget_join_cache(shop_id: int, telegram_id: int) -> None:
    """Make the next request ask Telegram again (the customer pressed "I joined")."""
    await kv.unmark(f"joined:{shop_id}:{telegram_id}")


async def forced_gate(db: AsyncSession, shop: Shop, customer: Customer) -> list[ForcedChannel]:
    """Channels this customer still has to join before using the Mini App (GRW-07).

    The verdict is cached per customer for a few minutes. Telegram errors never block anyone:
    the shared check flags the channel for the admin panel and the customer keeps working.
    """
    if await kv.marked([f"joined:{shop.id}:{customer.telegram_id}"]):
        return []
    rows = list(
        await db.scalars(
            select(ForcedChannel)
            .where(ForcedChannel.shop_id == shop.id, ForcedChannel.is_active.is_(True))
            .order_by(ForcedChannel.sort, ForcedChannel.id)
        )
    )
    if not rows:
        return []
    bot = await telegram.bot_for(shop.id)
    if bot is None:
        return []
    missing, _unknown = await channel_rules.missing_members(bot, rows, customer.telegram_id)
    if not missing:
        await kv.mark(f"joined:{shop.id}:{customer.telegram_id}", JOIN_CACHE_TTL)
    return missing


async def _gate_exempt(db: AsyncSession, shop_id: int, telegram_id: int) -> bool:
    """Same exemption as the bot's gate: active staff (not reseller accounts) and the owners."""
    from paurubot.db.models import Staff, StaffRole

    if telegram_id in get_settings().owner_telegram_ids:
        return True
    role = await db.scalar(
        select(Staff.role).where(
            Staff.shop_id == shop_id, Staff.telegram_id == telegram_id, Staff.is_active.is_(True)
        )
    )
    return role is not None and role != StaffRole.RESELLER


async def account_gate(
    db: AsyncSession, shop: Shop, customer: Customer, cfg: dict[str, Any], *, purchase: bool = False
) -> str | None:
    """What the customer must do before using the Mini App, exactly as the bot asks (SEC-F11).

    Phone first, then the current terms version. None when nothing is missing. Terms asked only
    «before purchase» count only for a purchase.
    """
    terms_now = cfg.get("terms_when") != "purchase" or purchase
    phone_now = cfg.get("phone_when") != "purchase" or purchase
    from paurubot.core import referral

    if cfg.get("referral_required") and await referral.needs_inviter(db, customer):
        need = "referral_required"  # named in the bot only: a link or the inviter's code
    elif cfg["require_phone"] and phone_now and not customer.phone:
        need = "phone_required"
    elif cfg["terms_required"] and terms_now and customer.terms_accepted_version != int(cfg["terms_version"]):
        need = "terms_required"
    else:
        return None
    return None if await _gate_exempt(db, shop.id, customer.telegram_id) else need


async def reseller_tokens(db: AsyncSession) -> list[tuple[int, str]]:
    global _reseller_tokens_cache
    now = time.monotonic()
    if _reseller_tokens_cache is not None and now - _reseller_tokens_cache[0] < RESELLER_TOKENS_TTL:
        return _reseller_tokens_cache[1]
    from paurubot.core import resale

    tokens = [(shop.id, token) for shop, token in await resale.bots(db)]
    _reseller_tokens_cache = (now, tokens)
    return tokens


async def webapp_context(request: Request, db: DB) -> WebAppContext:
    settings = get_settings()
    raw = request.headers.get(HEADER, "")
    main_token = settings.bot_token.get_secret_value()
    try:
        try:
            user, shop_id = validate_init_data(raw, main_token, max_age=settings.webapp_auth_max_age), None
        except InitDataError:
            if not raw:
                raise
            user, shop_id = match_init_data(
                raw, main_token, await reseller_tokens(db), max_age=settings.webapp_auth_max_age
            )
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail="invalid_init_data") from exc
    allowed, _ = await kv.hit(f"webapp:{user.id}", RATE_LIMIT, RATE_WINDOW)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate_limited")
    if shop_id is None:
        shop = await root_shop(db)
        if shop is None:
            raise HTTPException(status_code=503, detail="not_set_up")
    else:
        shop = await db.get(Shop, shop_id)
        # The token list is cached for a minute; a shop paused since then must not serve.
        if shop is None or shop.status != ShopStatus.ACTIVE:
            raise HTTPException(status_code=401, detail="invalid_init_data")
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
    path = request.url.path.rstrip("/")
    if path not in JOIN_PATHS:
        missing = await forced_gate(db, shop, customer)
        if missing:
            raise HTTPException(status_code=403, detail="join_required")
        if path not in GATE_PATHS:
            # The bot refuses everything until phone and terms are done; so does every Mini App
            # route, so a purchase can never skip what the shop requires (SEC-F11).
            purchase = request.method == "POST" and path == "/api/webapp/orders"
            need = await account_gate(db, shop, customer, cfg, purchase=purchase)
            if need is not None:
                raise HTTPException(status_code=403, detail=need)
    return WebAppContext(db, shop, customer, cfg)


WebApp = Annotated[WebAppContext, Depends(webapp_context)]
