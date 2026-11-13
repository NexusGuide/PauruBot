"""Shared Redis client, distributed locks and fixed-window rate limits (ARC-02)."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

from redis.asyncio import BlockingConnectionPool, Redis

from paurubot.config import get_settings

_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

_client: Redis | None = None

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def make_client(url: str | None = None, *, max_connections: int | None = None, **options: Any) -> Redis:
    """A Redis client whose pool makes a caller wait for a free connection when all are in use.

    The default pool raises "Too many connections" at once, so a burst of bot updates failed
    instead of waiting a few milliseconds.
    """
    settings = get_settings()
    pool = BlockingConnectionPool.from_url(
        url or settings.redis_url,
        max_connections=max_connections or settings.redis_max_connections,
        timeout=settings.redis_pool_timeout,
        **options,
    )
    return Redis(connection_pool=pool)


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = make_client(decode_responses=True)
    return _client


def set_redis(client: Redis | None) -> None:
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose(close_connection_pool=True)
    _client = None


MARK_PREFIX = "mark:"


async def mark(name: str, ttl: int, value: str = "1") -> None:
    """Remember a short-lived fact (a check result, a delivery failure) for other processes."""
    await get_redis().set(f"{MARK_PREFIX}{name}", value or "1", ex=ttl)


async def unmark(name: str) -> None:
    await get_redis().delete(f"{MARK_PREFIX}{name}")


async def marked(names: Sequence[str]) -> dict[str, str]:
    """The subset of these marks that is set, with the value it was stored with."""
    if not names:
        return {}
    values = await get_redis().mget([f"{MARK_PREFIX}{name}" for name in names])
    return {name: str(value) for name, value in zip(names, values, strict=True) if value is not None}


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


class LockLost(LockNotAcquired):
    """An in-flight operation no longer owns its renewable lock."""


@asynccontextmanager
async def lease(name: str, ttl: int = 60, *, wait_seconds: float = 0.0) -> AsyncIterator[str]:
    """Renew an owner-checked lock; cancel cooperative work if ownership cannot be proved.

    This is not fencing of a remote API. Callers must persist ambiguous operations and must
    not refund or blindly repeat a remote mutation after cancellation/network failure.
    """
    if ttl < 3:
        raise ValueError("lease ttl must be at least three seconds")
    caller = asyncio.current_task()
    assert caller is not None
    lost = False
    held = (
        lock(name, ttl=ttl, wait=True, wait_seconds=wait_seconds) if wait_seconds > 0 else lock(name, ttl=ttl)
    )
    async with held as token:

        async def keep() -> None:
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(ttl / 3)
                    renewed = await get_redis().eval(_RENEW, 1, f"lock:{name}", token, ttl)
                    if not renewed:
                        raise LockLost(name)
            except Exception:
                # Fail closed on an expired/stolen key AND on an unavailable Redis server.
                lost = True
                caller.cancel()

        keeper = asyncio.create_task(keep())
        try:
            yield token
            # A callee may consume cancellation while cleaning up a remote request.
            # That must never turn a lost lease into successful completion.
            if lost:
                raise LockLost(name)
        except asyncio.CancelledError as exc:
            if lost:
                raise LockLost(name) from exc
            raise
        finally:
            keeper.cancel()
            with suppress(asyncio.CancelledError):
                await keeper


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


async def forget_once(key: str) -> None:
    """Undo `set_once`, so the next attempt counts as the first again."""
    await get_redis().delete(f"once:{key}")


async def seen_once(key: str) -> bool:
    """Whether a successful operation has already been remembered."""
    return bool(await get_redis().exists(f"once:{key}"))


_COMPLETE_ONCE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('set', KEYS[2], '1', 'EX', ARGV[2])
    return 1
end
return 0
"""


async def complete_once(key: str, owner: str, ttl: int) -> None:
    """Mark work complete only while the caller still owns its in-flight lease."""
    completed = await get_redis().eval(_COMPLETE_ONCE, 2, f"lock:{key}", f"once:{key}", owner, ttl)
    if not completed:
        raise LockNotAcquired(key)
