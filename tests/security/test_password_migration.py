"""Versioned password hashing: preserve every byte and retain explicit legacy compatibility."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from paurubot.api.app import create_app
from paurubot.auth import staff as staff_service
from paurubot.core.setup import issue_token
from paurubot.db.models import Staff
from paurubot.security.tokens import hash_password, verify_password

pytestmark = pytest.mark.security


@pytest.mark.parametrize("prefix", ["a" * 72, "ش" * 36, "🔑" * 18])
def test_new_passwords_do_not_alias_after_byte_72(prefix: str) -> None:
    original = prefix + "original-tail"
    hashed = hash_password(original)
    assert verify_password(original, hashed)
    assert not verify_password(prefix + "different-tail", hashed)
    assert not verify_password(prefix, hashed)


def test_hash_is_self_describing_argon2id_and_salted() -> None:
    first = hash_password("correct horse battery")
    second = hash_password("correct horse battery")
    assert first.startswith("$argon2id$v=19$")
    assert first != second
    assert len(first) <= 255  # existing Staff.password_hash column


@pytest.mark.parametrize("hashed", [None, "", "not-a-hash", "$argon2id$bad", "$2b$bad", "$unknown$x"])
def test_missing_or_invalid_hash_fails_closed(hashed: str | None) -> None:
    assert not verify_password("some candidate password", hashed)


# A bcrypt hash checks only the first 72 bytes of a password, so any suffix logged in. Nothing
# is installed yet and no older release wrote one, so the old reader is gone (AUD-10). Real
# hashes, made once with bcrypt: of "a" * 10, and of "a" * 72.
BCRYPT_OF_TEN_A = "$2b$04$ekOhIfXIgflkAjbiR6C9r.83tRt7sRbm0Qr5qxNcGbktFADjTj3re"
BCRYPT_OF_72_A = "$2b$04$0kKjPTgwjpCQEd/DPZjg1OXVu4WNCqtCaBY4duxhzKPMo5XpMI2IC"


@pytest.mark.parametrize("prefix", ["$2a$", "$2b$", "$2y$"])
def test_a_bcrypt_hash_never_verifies_even_with_the_right_password(prefix: str) -> None:
    assert not verify_password("a" * 10, prefix + BCRYPT_OF_TEN_A[4:])
    assert not verify_password("a" * 72 + "any suffix", BCRYPT_OF_72_A)


async def test_a_bcrypt_row_cannot_log_in_but_an_admin_can_set_a_new_password(session) -> None:  # type: ignore[no-untyped-def]
    app = create_app()
    transport = httpx.ASGITransport(app=app, client=("10.1.2.3", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await issue_token()
        response = await client.post(
            "/api/setup/complete",
            json={
                "token": token,
                "shop_title": "Passwords",
                "username": "owner",
                "password": "a" * 72 + "legacy-tail",
            },
        )
        assert response.status_code == 200, response.text
        row = await session.scalar(select(Staff).where(Staff.username == "owner"))
        assert row is not None
        row.password_hash = BCRYPT_OF_72_A
        await session.commit()
        response = await client.post(
            "/api/auth/login", json={"username": "owner", "password": "a" * 72 + "anything"}
        )
        assert response.status_code == 401, response.text
        replacement = "ش" * 40 + "fresh-tail"
        await staff_service.change_password(session, row, replacement)
        await session.commit()
        await session.refresh(row)
        assert row.password_hash.startswith("$argon2id$")
        logged = await staff_service.authenticate(session, "owner", replacement, "10.1.2.4")
        assert logged.id == row.id
