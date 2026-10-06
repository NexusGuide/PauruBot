from __future__ import annotations

import json
import logging
import time

import pytest
from sqlalchemy import text

from paurubot.security import secretbox, ssrf
from paurubot.security.redact import RedactingFilter, redact, register_secret
from paurubot.security.tokens import (
    InitDataError,
    constant_equals,
    hash_password,
    sign_init_data,
    validate_init_data,
    verify_password,
)

pytestmark = pytest.mark.security

BOT_TOKEN = "123456:TEST-token-for-unit-tests-only-xxxxxxxx"


def test_redacts_bot_token_password_and_bearer() -> None:
    raw = (
        "token 7012345678:AAH9xYzAbCdEfGhIjKlMnOpQrStUvWxYz12345 "
        'password="hunter2pass" Authorization: Bearer abc.def.ghijkl '
        "https://admin:s3cret@panel.example.com/api"
    )
    out = redact(raw)
    assert "AAH9xYz" not in out
    assert "hunter2pass" not in out
    assert "abc.def.ghijkl" not in out
    assert "s3cret" not in out


def test_registered_secret_is_redacted_from_logs() -> None:
    register_secret("panel-pass-77ab")
    record = logging.makeLogRecord({"msg": "login with %s failed", "args": ("panel-pass-77ab",)})
    RedactingFilter().filter(record)
    assert "panel-pass-77ab" not in record.getMessage()


def test_secretbox_round_trip_and_domain_separation() -> None:
    token = secretbox.encrypt("panel", "s3cr3t-value")
    assert secretbox.decrypt("panel", token) == "s3cr3t-value"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt("payment", token)
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.encrypt("unknown-purpose", "x")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "http://169.254.169.254/latest/meta-data",
        "http://[::ffff:169.254.169.254]/",
        "http://127.0.0.1:8000",
        "http://10.0.0.5",
        "http://user:pass@example.com",
        "http://0.0.0.0",
    ],
)
async def test_ssrf_blocks_dangerous_urls(url: str) -> None:
    with pytest.raises(ssrf.UnsafeUrl):
        await ssrf.assert_safe_url(url)


async def test_ssrf_private_needs_explicit_approval_but_metadata_never() -> None:
    assert await ssrf.assert_safe_url("http://127.0.0.1:2053", allow_private=True) == ["127.0.0.1"]
    with pytest.raises(ssrf.UnsafeUrl):
        await ssrf.assert_safe_url("http://169.254.169.254", allow_private=True)


async def test_ssrf_allows_public_ip() -> None:
    assert await ssrf.assert_safe_url("https://1.1.1.1/") == ["1.1.1.1"]


def _init_data(auth_date: int, user_id: int = 42) -> str:
    return sign_init_data(
        {
            "auth_date": str(auth_date),
            "query_id": "AAE",
            "user": json.dumps({"id": user_id, "first_name": "A"}),
        },
        BOT_TOKEN,
    )


def test_init_data_valid() -> None:
    now = int(time.time())
    user = validate_init_data(_init_data(now), BOT_TOKEN, max_age=3600)
    assert user.id == 42


def test_init_data_rejects_tampering_expiry_and_wrong_bot() -> None:
    now = int(time.time())
    tampered = _init_data(now).replace("%22id%22%3A+42", "%22id%22%3A+43")
    with pytest.raises(InitDataError):
        validate_init_data(tampered, BOT_TOKEN, max_age=3600)
    with pytest.raises(InitDataError):
        validate_init_data(_init_data(now - 7200), BOT_TOKEN, max_age=3600)
    with pytest.raises(InitDataError):
        validate_init_data(_init_data(now), "999:other-bot-token", max_age=3600)
    with pytest.raises(InitDataError):
        validate_init_data("", BOT_TOKEN, max_age=3600)


def test_passwords_and_constant_compare() -> None:
    hashed = hash_password("correct horse battery")
    assert verify_password("correct horse battery", hashed)
    assert not verify_password("wrong password!!", hashed)
    assert not verify_password("anything at all", None)
    with pytest.raises(ValueError):
        hash_password("short")
    assert constant_equals("abc", "abc") and not constant_equals("abc", "abd")
    assert not constant_equals(None, "x")


async def test_ledger_tables_are_append_only(session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Exception, match="append-only"):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM ledger_entries"))
    with pytest.raises(Exception, match="append-only"):
        async with session.begin_nested():
            await session.execute(text("UPDATE ledger_journals SET memo = 'x'"))
