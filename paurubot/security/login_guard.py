"""Login brute-force guard and trusted-hop client IP resolution (SEC-07)."""

from __future__ import annotations

import ipaddress

from paurubot import kv
from paurubot.config import get_settings
from paurubot.security.tokens import sha256_hex


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


# A failed sign-in counts against the address it came from, the address alone and the account
# (audit SEC-01). The pair locks first, so a guesser locks out only itself; the account locks only
# when many addresses guess it together. Keys hold digests, never a raw name or address.
IP_FACTOR = 4  # offices and CGNAT share addresses
ACCOUNT_FACTOR = 10


def _name(username: str) -> str:
    return sha256_hex(username.strip().lower())[:32]


def _keys(username: str, ip: str) -> tuple[tuple[str, int], ...]:
    name, addr = _name(username), sha256_hex(ip)[:32]
    return (
        (f"login:pair:{name}:{addr}", 1),
        (f"login:ip:{addr}", IP_FACTOR),
        (f"login:user:{name}", ACCOUNT_FACTOR),
    )


async def check(username: str, ip: str) -> None:
    redis = kv.get_redis()
    for key, _ in _keys(username, ip):
        ttl = await redis.ttl(f"loginlock:{key}")
        if ttl and ttl > 0:
            raise LoginLocked(ttl)


async def failed(username: str, ip: str) -> None:
    settings = get_settings()
    redis = kv.get_redis()
    for key, factor in _keys(username, ip):
        limit = settings.login_max_attempts * factor
        _, count = await kv.hit(key, limit, settings.login_lock_seconds)
        if count >= limit:
            await redis.set(f"loginlock:{key}", "1", ex=settings.login_lock_seconds)


async def succeeded(username: str, ip: str) -> None:
    pair, _, account = (key for key, _ in _keys(username, ip))
    await kv.reset(pair)
    await kv.reset(account)


async def allow_send(kind: str, username: str, ip: str, *, per_address: int, window: int = 600) -> None:
    """Throttle codes and reset links sent to an account's Telegram, without one requester's
    flood blocking the account's owner: the budget is per requesting address, and the account
    as a whole takes a few times more before it stops answering."""
    name, addr = _name(username), sha256_hex(ip)[:32]
    for key, limit in (
        (f"send:{kind}:pair:{name}:{addr}", per_address),
        (f"send:{kind}:user:{name}", per_address * 4),
    ):
        allowed, _ = await kv.hit(key, limit, window)
        if not allowed:
            raise LoginLocked(window)
