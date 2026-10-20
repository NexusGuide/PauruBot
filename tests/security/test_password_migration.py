"""Versioned password hashing: preserve every byte and retain explicit legacy compatibility."""

from __future__ import annotations

import bcrypt
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


@pytest.mark.parametrize("password", ["a" * 10, "ش" * 40, "b" * 72 + "old suffix"])
def test_legacy_bcrypt_still_works_without_silent_rehash(password: str) -> None:
    legacy = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
    assert verify_password(password, legacy)
    assert not verify_password("different beginning", legacy)


async def test_legacy_login_then_explicit_password_change(session) -> None:  # type: ignore[no-untyped-def]
    app = create_app()
    transport = httpx.ASGITransport(app=app, client=("10.1.2.3", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        old = "a" * 72 + "legacy-tail"
        token = await issue_token()
        response = await client.post(
            "/api/setup/complete",
            json={
                "token": token,
                "shop_title": "Passwords",
                "username": "owner",
                "password": old,
            },
        )
        assert response.status_code == 200, response.text
        row = await session.scalar(select(Staff).where(Staff.username == "owner"))
        assert row is not None
        legacy = bcrypt.hashpw(old.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
        row.password_hash = legacy
        await session.commit()
        response = await client.post("/api/auth/login", json={"username": "owner", "password": old})
        assert response.status_code == 200, response.text
        await session.refresh(row)
        assert row.password_hash == legacy  # never bless an ambiguous legacy suffix on login
        replacement = "ش" * 40 + "fresh-tail"
        response = await client.post(
            "/api/auth/password",
            headers={
                "x-csrf-token": response.json()["csrf_token"],
            },
            json={"current_password": old, "new_password": replacement},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "legacy_password_reset_required"
        await session.refresh(row)
        assert row.password_hash == legacy
        # An independently authorized administrator can still explicitly rotate the secret.
        # Public Telegram recovery is tested separately; login alone never blesses a suffix.
        await staff_service.change_password(session, row, replacement)
        await session.commit()
        await session.refresh(row)
        assert row.password_hash.startswith("$argon2id$")
        assert verify_password(replacement, row.password_hash)
        assert not verify_password("ش" * 40 + "another-tail", row.password_hash)
        assert row.session_version == 2
        # Both the current-password check and normal staff authentication use the new verifier.
        logged = await staff_service.authenticate(session, "owner", replacement, "10.1.2.4")
        assert logged.id == row.id
