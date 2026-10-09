"""Login brute-force guard and trusted-hop client IP resolution (SEC-07)."""

from __future__ import annotations

import ipaddress

from paurubot import kv
from paurubot.config import get_settings


def client_ip(peer: str | None, forwarded_for: str | None, trusted_hops: int | None = None) -> str:
    """Pick the client address added by our own last trusted proxy, never the left-most XFF entry.

    With N trusted proxies the real client is the N-th address from the right of
    `X-Forwarded-For`; anything further left was supplied by the client and is ignored.
    """
    hops = get_settings().trust_proxy_hops if trusted_hops is None else trusted_hops
    if hops <= 0 or not forwarded_for:
        return peer or "unknown"
    chain = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(chain) < hops:
        return peer or "unknown"
    candidate = chain[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer or "unknown"


class LoginLocked(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


def _keys(username: str, ip: str) -> tuple[str, str]:
    return f"login:user:{username.lower()}", f"login:ip:{ip}"


async def check(username: str, ip: str) -> None:
    redis = kv.get_redis()
    for key in _keys(username, ip):
        ttl = await redis.ttl(f"loginlock:{key}")
        if ttl and ttl > 0:
            raise LoginLocked(ttl)


async def failed(username: str, ip: str) -> None:
    settings = get_settings()
    redis = kv.get_redis()
    for index, key in enumerate(_keys(username, ip)):
        # IPs get a wider budget because offices and CGNAT share addresses.
        limit = settings.login_max_attempts * (4 if index else 1)
        _, count = await kv.hit(key, limit, settings.login_lock_seconds)
        if count >= limit:
            await redis.set(f"loginlock:{key}", "1", ex=settings.login_lock_seconds)


async def succeeded(username: str, ip: str) -> None:
    user_key, ip_key = _keys(username, ip)
    await kv.reset(user_key)
    await kv.reset(ip_key)
