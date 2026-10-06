"""Server-side staff sessions in Redis, bound to the staff session_version (SEC-04)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from paurubot import kv
from paurubot.config import get_settings
from paurubot.security.tokens import new_token, sha256_hex

PREFIX = "session:"


@dataclass(slots=True)
class StaffSession:
    staff_id: int
    shop_id: int
    session_version: int
    csrf: str
    created_at: float
    ip: str


def _key(session_id: str) -> str:
    # Only a hash of the cookie value is stored, so a Redis dump cannot be replayed as cookies.
    return PREFIX + sha256_hex(session_id)


async def create(staff_id: int, shop_id: int, session_version: int, ip: str) -> tuple[str, StaffSession]:
    session_id = new_token(32)
    data = StaffSession(staff_id, shop_id, session_version, new_token(24), time.time(), ip)
    ttl = get_settings().session_ttl_seconds
    redis = kv.get_redis()
    await redis.set(_key(session_id), json.dumps(asdict(data)), ex=ttl)
    await redis.sadd(f"staff_sessions:{staff_id}", _key(session_id))
    await redis.expire(f"staff_sessions:{staff_id}", ttl)
    return session_id, data


async def load(session_id: str | None) -> StaffSession | None:
    if not session_id or len(session_id) > 128:
        return None
    raw = await kv.get_redis().get(_key(session_id))
    if not raw:
        return None
    try:
        return StaffSession(**json.loads(raw))
    except (TypeError, ValueError):
        return None


async def destroy(session_id: str | None) -> None:
    if session_id:
        await kv.get_redis().delete(_key(session_id))


async def destroy_all(staff_id: int) -> None:
    redis = kv.get_redis()
    keys = await redis.smembers(f"staff_sessions:{staff_id}")
    if keys:
        await redis.delete(*keys)
    await redis.delete(f"staff_sessions:{staff_id}")
