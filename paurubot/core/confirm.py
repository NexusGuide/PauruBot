"""Two-step confirmation for destructive staff actions (F-22).

The first step stores a short-lived challenge in Redis under ``confirm:{actor}:{action}:{target}``;
the second step must bring back the token, and on the web also type the phrase shown (a service's
username, the number of services in a bulk run, a refund's amount). A challenge is single-use: it is
gone after the first attempt, right or wrong, so it can't be guessed or replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from paurubot import kv

TTL_SECONDS = 300
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)


@dataclass(frozen=True, slots=True)
class Challenge:
    token: str
    phrase: str
    expires_in: int

    def as_dict(self) -> dict[str, Any]:
        return {"token": self.token, "phrase": self.phrase, "expires_in": self.expires_in}


def normalize(text: Any) -> str:
    """Persian/Arabic digits, case, spaces, separators and a leading @ don't matter when typing."""
    value = str(text or "").translate(_DIGITS).strip().lstrip("@").casefold()
    return " ".join(value.replace(",", "").replace("٬", "").split())


def key(actor: str, action: str, target: str | int) -> str:
    return f"confirm:{actor}:{action}:{target}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def selection_digest(action: str, ids: list[int]) -> str:
    """A stable target for a bulk run: confirming one selection never confirms another."""
    raw = action + ":" + ",".join(str(i) for i in sorted(set(ids)))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def _store(name: str, token: str, phrase: str, ttl: int) -> None:
    payload = json.dumps({"t": _hash(token), "p": normalize(phrase)})
    await kv.get_redis().set(name, payload, ex=ttl)


async def _take(name: str) -> dict[str, Any] | None:
    async with kv.get_redis().pipeline(transaction=True) as pipe:
        pipe.get(name)
        pipe.delete(name)
        raw, _ = await pipe.execute()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def issue(
    actor: str, action: str, target: str | int, phrase: str, *, ttl: int = TTL_SECONDS
) -> Challenge:
    token = secrets.token_urlsafe(18)
    await _store(key(actor, action, target), token, phrase, ttl)
    return Challenge(token=token, phrase=str(phrase), expires_in=ttl)


async def consume(
    actor: str,
    action: str,
    target: str | int,
    token: str | None,
    typed: str | None,
    *,
    phrase: str | None = None,
) -> bool:
    """True once for the matching token and typed phrase.

    ``phrase`` is the phrase computed now; when it differs from the one issued (the service was
    renamed, the refund amount changed) the old confirmation is void.
    """
    if not token:
        return False
    data = await _take(key(actor, action, target))
    if data is None or not hmac.compare_digest(str(data.get("t", "")), _hash(token)):
        return False
    expected = str(data.get("p", ""))
    if phrase is not None and normalize(phrase) != expected:
        return False
    return hmac.compare_digest(normalize(typed), expected)


async def issue_nonce(actor: str, action: str, target: str | int, *, ttl: int = TTL_SECONDS) -> int:
    """A one-time number for a bot's second confirm button (it fits callback data)."""
    nonce = secrets.randbelow(2**31 - 2) + 1
    await _store(key(actor, action, target), str(nonce), "", ttl)
    return nonce


async def consume_nonce(actor: str, action: str, target: str | int, nonce: int) -> bool:
    if nonce <= 0:
        return False
    data = await _take(key(actor, action, target))
    return data is not None and hmac.compare_digest(str(data.get("t", "")), _hash(str(nonce)))
