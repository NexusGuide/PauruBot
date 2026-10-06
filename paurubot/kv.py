"""Shared Redis client, distributed locks and fixed-window rate limits (ARC-02)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from paurubot.config import get_settings

_client: Redis | None = None

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_redis(client: Redis | None) -> None:
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


class LockNotAcquired(Exception):
    pass


@asynccontextmanager
async def lock(
    name: str, ttl: int = 30, *, wait: bool = False, wait_seconds: float = 10.0
) -> AsyncIterator[str]:
    """Owner-checked lock: only the holder's token can release it."""
    import asyncio

    redis = get_redis()
    key = f"lock:{name}"
    token = secrets.token_hex(16)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while not await redis.set(key, token, nx=True, ex=ttl):
        if not wait or asyncio.get_running_loop().time() > deadline:
            raise LockNotAcquired(name)
        await asyncio.sleep(0.1)
    try:
        yield token
    finally:
        await redis.eval(_RELEASE, 1, key, token)


async def hit(key: str, limit: int, window: int) -> tuple[bool, int]:
    """Count one event; return (allowed, current_count) for a fixed window of `window` seconds."""
    redis = get_redis()
    full = f"rl:{key}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(full)
        pipe.expire(full, window, nx=True)
        count, _ = await pipe.execute()
    return int(count) <= limit, int(count)


async def reset(key: str) -> None:
    await get_redis().delete(f"rl:{key}")


async def set_once(key: str, ttl: int) -> bool:
    """True the first time `key` is set within `ttl` seconds, False after that."""
    return bool(await get_redis().set(f"once:{key}", "1", nx=True, ex=ttl))
