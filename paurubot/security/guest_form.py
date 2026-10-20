"""Short-lived signed guest form identity; pair with the same HttpOnly host cookie.

Never a purchase-page token. Retain the same identity on GET/reload; only an explicit
new-purchase action gets another identity. Database tombstones outlive this token.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from paurubot.config import get_settings

TTL = 1800
TOKEN = re.compile(r"([1-9][0-9]{0,18})\.([0-9]{1,12})\.([A-Za-z0-9_-]{43})\.([0-9a-f]{64})\Z")


def _sign(value: str) -> str:
    key = get_settings().master_key.get_secret_value()
    if len(key) < 32:
        raise ValueError("guest checkout requires a configured master key")
    return hmac.new(key.encode(), b"guest-checkout-form:v1:" + value.encode(), hashlib.sha256).hexdigest()


def _now() -> int:
    return int(time.time())


def issue(shop_id: int) -> str:
    value = f"{shop_id}.{_now()}.{secrets.token_urlsafe(32)}"
    return value + "." + _sign(value)


def valid(token: str, shop_id: int) -> bool:
    if not isinstance(token, str) or len(token) > 160:
        return False
    match = TOKEN.fullmatch(token)
    if match is None or int(match[1]) != shop_id:
        return False
    age = _now() - int(match[2])
    if not 0 <= age < TTL:
        return False
    return hmac.compare_digest(match[4], _sign(token.rsplit(".", 1)[0]))


def matches(token: str, cookie: str, shop_id: int) -> bool:
    return valid(token, shop_id) and valid(cookie, shop_id) and hmac.compare_digest(token, cookie)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
