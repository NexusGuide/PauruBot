"""Passwords, constant-time comparisons, Telegram initData and webhook secrets (SEC-01, SEC-11)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
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


_PASSWORD_WORKERS = asyncio.Semaphore(2)


async def _password_work(func: Callable[..., Any], *args: Any) -> Any:
    workers = _PASSWORD_WORKERS
    await workers.acquire()
    try:
        task = asyncio.create_task(asyncio.to_thread(func, *args))
    except BaseException:
        workers.release()
        raise

    def finished(result: asyncio.Task[Any]) -> None:
        # The caller may be cancelled repeatedly; only the actual worker frees its slot.
        workers.release()
        if not result.cancelled():
            result.exception()  # Observe failures even when the caller has gone away.

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def hash_password_async(password: str) -> str:
    return str(await _password_work(hash_password, password))


async def verify_password_async(password: str, hashed: str | None) -> bool:
    return bool(await _password_work(verify_password, password, hashed))


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


def _signed_fields(
    init_data: str, bot_token: str, *, max_age: int, now: float | None
) -> tuple[dict[str, str], int]:
    """The fields of a WebApp-signed query string and its auth_date, once hash and age check out."""
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
    return pairs, auth_date


def validate_init_data(
    init_data: str, bot_token: str, *, max_age: int, now: float | None = None
) -> WebAppUser:
    """Validate Telegram Mini App initData per the WebApp spec, including auth_date freshness."""
    pairs, auth_date = _signed_fields(init_data, bot_token, max_age=max_age, now=now)
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


@dataclass(slots=True)
class SharedContact:
    user_id: int
    phone: str


def validate_shared_contact(
    response: str, bot_token: str, *, max_age: int, now: float | None = None
) -> SharedContact:
    """The contact a Mini App got back from `requestContact` (F-12), checked like initData.

    Telegram signs the shared contact with the same WebAppData key as initData, so the Mini App
    can hand it to us directly instead of waiting for the bot to receive the contact message.
    """
    pairs, _ = _signed_fields(response, bot_token, max_age=max_age, now=now)
    try:
        contact = json.loads(pairs.get("contact", "{}"))
    except json.JSONDecodeError as exc:
        raise InitDataError("bad contact payload") from exc
    if (
        not isinstance(contact, dict)
        or not isinstance(contact.get("user_id"), int)
        or not isinstance(contact.get("phone_number"), str)
    ):
        raise InitDataError("missing contact")
    return SharedContact(user_id=contact["user_id"], phone=contact["phone_number"])


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Build a valid initData string (tests and local tooling)."""
    from urllib.parse import urlencode

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})
