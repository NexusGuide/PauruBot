"""First-run setup gate (SEC-08): one-time token from the operator console creates the owner."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.audit.log import Actor, record
from paurubot.auth.staff import create_staff
from paurubot.core import settings as shop_settings
from paurubot.core.shops import create_shop, is_set_up
from paurubot.db.models import Shop, Staff, StaffRole
from paurubot.security.tokens import constant_equals, new_token, sha256_hex

TOKEN_KEY = "setup:token_sha256"
TOKEN_TTL = 24 * 3600


class SetupError(Exception):
    pass


async def issue_token() -> str:
    token = new_token(24)
    await kv.get_redis().set(TOKEN_KEY, sha256_hex(token), ex=TOKEN_TTL)
    return token


async def token_valid(token: str) -> bool:
    stored = await kv.get_redis().get(TOKEN_KEY)
    return bool(stored) and constant_equals(stored, sha256_hex(token))


async def complete(
    session: AsyncSession,
    *,
    token: str,
    shop_title: str,
    username: str,
    password: str,
    telegram_id: int | None,
    ip: str,
) -> tuple[Shop, Staff]:
    async with kv.lock("setup", ttl=30):
        if await is_set_up(session):
            raise SetupError("already_set_up")
        if not await token_valid(token):
            raise SetupError("invalid_token")
        shop = await create_shop(session, shop_title)
        owner = await create_staff(
            session,
            shop.id,
            role=StaffRole.OWNER,
            username=username,
            password=password,
            telegram_id=telegram_id,
            display_name=username,
        )
        await shop_settings.set_many(session, shop.id, {})
        await record(
            session,
            shop_id=shop.id,
            actor=Actor("system", None, ip),
            action="setup.completed",
            entity="shop",
            entity_id=shop.id,
        )
        await session.commit()
        await kv.get_redis().delete(TOKEN_KEY)
    return shop, owner
