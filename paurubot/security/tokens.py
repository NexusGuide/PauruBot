"""Passwords, constant-time comparisons, Telegram initData and webhook secrets (SEC-01, SEC-11)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16, type=Type.ID
)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    return _PASSWORD_HASHER.hash(password)


_DUMMY_HASH = _PASSWORD_HASHER.hash("timing-equaliser")


def verify_password(password: str, hashed: str | None) -> bool:
    """Read versioned hashes; legacy bcrypt retains its historical 72-byte semantics.

    A legacy hash cannot tell us whether the original password had a suffix. Do not
    silently rehash it on login: only an explicit password change establishes a new secret.
    """
    if not hashed:
        try:
            _PASSWORD_HASHER.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        return False
    try:
        if hashed.startswith("$argon2id$"):
            return _PASSWORD_HASHER.verify(hashed, password)
        if hashed.startswith(("$2a$", "$2b$", "$2y$")):
            return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except (InvalidHashError, VerificationError, ValueError):
        return False
    return False


def constant_equals(a: str | bytes | None, b: str | bytes | None) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class InitDataError(ValueError):
    pass


@dataclass(slots=True)
class WebAppUser:
    id: int
    first_name: str | None
    username: str | None
    language_code: str | None
    auth_date: int
    raw: dict[str, Any]


def validate_init_data(
    init_data: str, bot_token: str, *, max_age: int, now: float | None = None
) -> WebAppUser:
    """Validate Telegram Mini App initData per the WebApp spec, including auth_date freshness."""
    if not init_data or not bot_token:
        raise InitDataError("missing init data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("missing hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def digest(fields: dict[str, str]) -> str:
        check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # Telegram's hash covers every field but `hash`, the Ed25519 `signature` included; only the
    # third-party check leaves `signature` out. Both forms are keyed with the bot token.
    candidates = [pairs]
    if "signature" in pairs:
        candidates.append({k: v for k, v in pairs.items() if k != "signature"})
    if not any(hmac.compare_digest(digest(fields), received) for fields in candidates):
        raise InitDataError("bad signature")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("bad auth_date") from exc
    current = time.time() if now is None else now
    if auth_date <= 0 or current - auth_date > max_age or auth_date - current > 60:
        raise InitDataError("init data expired")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise InitDataError("bad user payload") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("missing user")
    return WebAppUser(
        id=user["id"],
        first_name=user.get("first_name"),
        username=user.get("username"),
        language_code=user.get("language_code"),
        auth_date=auth_date,
        raw=user,
    )


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Build a valid initData string (tests and local tooling)."""
    from urllib.parse import urlencode

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})
