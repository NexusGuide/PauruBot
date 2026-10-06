"""Encryption of secrets at rest with per-purpose keys derived from MASTER_KEY (SEC-09).

Each purpose ("panel", "bot-token", "payment", ...) gets its own Fernet key through HKDF, so a
ciphertext from one domain can never be decrypted as another.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from paurubot.config import get_settings
from paurubot.security.redact import register_secret

PURPOSES = frozenset({"panel", "bot-token", "payment", "webhook", "backup-key", "totp"})
_SALT = b"paurubot/secretbox/v1"


class SecretBoxError(Exception):
    pass


def _master() -> bytes:
    key = get_settings().master_key.get_secret_value()
    if len(key) < 32:
        raise SecretBoxError("MASTER_KEY is missing or shorter than 32 characters")
    return key.encode()


@lru_cache(maxsize=32)
def _fernet(master: bytes, purpose: str) -> Fernet:
    if purpose not in PURPOSES:
        raise SecretBoxError(f"unknown secret purpose {purpose!r}")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=purpose.encode()).derive(master)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(purpose: str, plaintext: str) -> str:
    return _fernet(_master(), purpose).encrypt(plaintext.encode()).decode()


def decrypt(purpose: str, token: str) -> str:
    try:
        value = _fernet(_master(), purpose).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretBoxError(
            f"cannot decrypt {purpose} secret (wrong MASTER_KEY or corrupted value)"
        ) from exc
    register_secret(value)
    return value


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(48)).decode()
