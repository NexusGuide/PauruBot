"""Streaming authenticated encryption for backup archives.

Layout: header | chunk* where header = MAGIC, version, scrypt parameters, salt and a nonce prefix, and
each chunk = 4-byte big-endian length | AES-256-GCM ciphertext. The nonce of chunk i is the prefix, i and
a final-chunk flag, and the header is authenticated with every chunk, so a reordered, truncated,
extended or edited archive fails to decrypt instead of restoring something partial.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"PBAK"
VERSION = 1
CHUNK = 1 << 20
HEADER = struct.Struct(">4sBBBB16s7s")
LENGTH = struct.Struct(">I")
MAX_CHUNK = CHUNK + 16


class CryptoError(Exception):
    """Wrong passphrase, or a damaged or tampered archive."""


def _key(passphrase: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    if not passphrase:
        raise CryptoError("BACKUP_PASSPHRASE is empty")
    return Scrypt(salt=salt, length=32, n=2**n_log2, r=r, p=p).derive(passphrase.encode())


def _nonce(prefix: bytes, index: int, final: bool) -> bytes:
    return prefix + struct.pack(">I", index) + (b"\x01" if final else b"\x00")


def _chunks(stream: BinaryIO) -> Iterator[tuple[bytes, bool]]:
    current = stream.read(CHUNK)
    while True:
        following = stream.read(CHUNK)
        yield current, not following
        if not following:
            return
        current = following


def encrypt_stream(source: BinaryIO, target: BinaryIO, passphrase: str, *, n_log2: int = 15) -> None:
    salt, prefix = os.urandom(16), os.urandom(7)
    header = HEADER.pack(MAGIC, VERSION, n_log2, 8, 1, salt, prefix)
    aead = AESGCM(_key(passphrase, salt, n_log2, 8, 1))
    target.write(header)
    for index, (plain, final) in enumerate(_chunks(source)):
        sealed = aead.encrypt(_nonce(prefix, index, final), plain, header)
        target.write(LENGTH.pack(len(sealed)))
        target.write(sealed)


def decrypt_stream(source: BinaryIO, target: BinaryIO, passphrase: str) -> None:
    header = source.read(HEADER.size)
    if len(header) != HEADER.size:
        raise CryptoError("not a PauruBot backup")
    magic, version, n_log2, r, p, salt, prefix = HEADER.unpack(header)
    if magic != MAGIC:
        raise CryptoError("not a PauruBot backup")
    if version != VERSION or not 10 <= n_log2 <= 20:
        raise CryptoError(f"unsupported backup format version {version}")
    aead = AESGCM(_key(passphrase, salt, n_log2, r, p))
    index, finished = 0, False
    while True:
        raw = source.read(LENGTH.size)
        if not raw:
            break
        if finished:
            raise CryptoError("data after the final chunk")
        if len(raw) != LENGTH.size:
            raise CryptoError("archive is truncated")
        (size,) = LENGTH.unpack(raw)
        if size > MAX_CHUNK:
            raise CryptoError("archive is damaged")
        sealed = source.read(size)
        if len(sealed) != size:
            raise CryptoError("archive is truncated")
        for final in (False, True):
            try:
                plain = aead.decrypt(_nonce(prefix, index, final), sealed, header)
            except InvalidTag:
                continue
            finished = final
            target.write(plain)
            break
        else:
            if index == 0:
                raise CryptoError("wrong passphrase or damaged archive")
            raise CryptoError("archive is damaged or was modified")
        index += 1
    if not finished:
        raise CryptoError("archive is truncated")


def encrypt_file(source: Path, target: Path, passphrase: str, *, n_log2: int = 15) -> None:
    with source.open("rb") as src, target.open("wb") as dst:
        encrypt_stream(src, dst, passphrase, n_log2=n_log2)


def decrypt_file(source: Path, target: Path, passphrase: str) -> None:
    with source.open("rb") as src, target.open("wb") as dst:
        decrypt_stream(src, dst, passphrase)
