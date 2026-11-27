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
    if not session_id:
        return
    redis = kv.get_redis()
    key = _key(session_id)
    data = await load(session_id)
    await redis.delete(key)
    if data is not None:
        await redis.srem(f"staff_sessions:{data.staff_id}", key)


async def list_for_staff(staff_id: int, current_session_id: str | None = None) -> list[dict[str, object]]:
    """Active browser sessions for one staff account, pruning expired keys from the index."""
    redis = kv.get_redis()
    set_key = f"staff_sessions:{staff_id}"
    raw_keys = sorted(await redis.smembers(set_key))
    if not raw_keys:
        return []
    keys = [k.decode() if isinstance(k, bytes) else str(k) for k in raw_keys]
    values = await redis.mget(keys)
    current_key = _key(current_session_id) if current_session_id else None
    stale: list[str] = []
    items: list[dict[str, object]] = []
    for key, raw in zip(keys, values, strict=True):
        if not raw:
            stale.append(key)
            continue
        try:
            data = StaffSession(**json.loads(raw))
        except (TypeError, ValueError):
            stale.append(key)
            continue
        items.append(
            {
                "id": key.removeprefix(PREFIX)[:12],
                "ip": data.ip,
                "created_at": data.created_at,
                "current": key == current_key,
            }
        )
    if stale:
        await redis.srem(set_key, *stale)
    items.sort(key=lambda row: (not bool(row["current"]), -float(row["created_at"])))
    return items


async def destroy_others(staff_id: int, keep_session_id: str) -> int:
    """Sign out every other browser session of this staff account, keeping `keep_session_id`."""
    redis = kv.get_redis()
    set_key = f"staff_sessions:{staff_id}"
    keep_key = _key(keep_session_id)
    raw_keys = await redis.smembers(set_key)
    others = [
        (k.decode() if isinstance(k, bytes) else str(k))
        for k in raw_keys
        if (k.decode() if isinstance(k, bytes) else str(k)) != keep_key
    ]
    if not others:
        return 0
    removed = int(await redis.delete(*others) or 0)
    await redis.srem(set_key, *others)
    return removed


async def destroy_all(staff_id: int) -> None:
    redis = kv.get_redis()
    keys = await redis.smembers(f"staff_sessions:{staff_id}")
    if keys:
        await redis.delete(*keys)
    await redis.delete(f"staff_sessions:{staff_id}")
