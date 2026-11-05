"""Encrypted backups (OPS-05), master key escrow (NEW-01) and restore onto an empty database."""

from __future__ import annotations

import asyncio
import io
import os
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from paurubot.config import get_settings
from paurubot.ops import backup
from paurubot.ops.crypto import CHUNK, CryptoError, decrypt_stream, encrypt_stream

PASS = "correct horse battery staple"


def _encrypt(data: bytes, passphrase: str = PASS) -> bytes:
    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, passphrase, n_log2=10)
    return out.getvalue()


def _decrypt(blob: bytes, passphrase: str = PASS) -> bytes:
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(blob), out, passphrase)
    return out.getvalue()


@pytest.mark.parametrize("length", [0, 10, CHUNK, CHUNK * 2 + 123])
def test_roundtrip(length: int) -> None:
    data = os.urandom(length)
    assert _decrypt(_encrypt(data)) == data


def test_wrong_passphrase_is_rejected() -> None:
    with pytest.raises(CryptoError, match="wrong passphrase"):
        _decrypt(_encrypt(b"secret"), "not it")


def test_truncation_reordering_and_tampering_are_detected() -> None:
    blob = _encrypt(os.urandom(CHUNK * 2 + 10))
    header = 31
    first_len = int.from_bytes(blob[header : header + 4], "big")
    first_end = header + 4 + first_len
    with pytest.raises(CryptoError, match="truncated"):
        _decrypt(blob[:first_end])  # dropped the remaining chunks
    with pytest.raises(CryptoError):
        _decrypt(blob[:-5])
    flipped = bytearray(blob)
    flipped[header + 10] ^= 1
    with pytest.raises(CryptoError):
        _decrypt(bytes(flipped))
    with pytest.raises(CryptoError):
        _decrypt(blob + blob[header:first_end])  # a replayed chunk after the final one
    with pytest.raises(CryptoError, match="not a PauruBot backup"):
        _decrypt(b"PK\x03\x04" + blob[4:])


@pytest.fixture
def backup_env(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_passphrase", SecretStr(PASS))
    return tmp_path


async def test_backup_verify_and_restore_on_an_empty_database(backup_env, engine) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    info = await backup.create(label="test")
    assert info.path.exists() and oct(info.path.stat().st_mode & 0o777) == "0o600"
    assert info.path.read_bytes()[:4] == b"PBAK"
    assert b"db.dump" not in info.path.read_bytes()  # nothing readable without the passphrase

    result = await backup.verify(info.path, full=True)
    assert result["master_key_included"] is True
    assert result["manifest"]["alembic_revision"]
    assert set(result["restored"]) == {"customers", "services", "ledger_entries"}

    with pytest.raises(backup.BackupError, match="wrong passphrase"):
        await backup.verify(info.path, passphrase="nope")

    # Restore onto a brand-new database, as on a fresh server.
    url = make_url(settings.database_url)
    target = f"paurubot_restore_{secrets.token_hex(3)}"
    admin = backup.pg_env()
    admin["PGDATABASE"] = "postgres"
    await backup._run("psql", "-qc", f'CREATE DATABASE "{target}"', env=admin)
    try:
        original = settings.database_url
        object.__setattr__(
            settings, "database_url", url.set(database=target).render_as_string(hide_password=False)
        )
        try:
            contents = await backup.restore(info.path)
        finally:
            object.__setattr__(settings, "database_url", original)
        assert contents.master_key == settings.master_key.get_secret_value()
        env = dict(admin, PGDATABASE=target)
        revision = (
            await backup._run("psql", "-Atqc", "SELECT version_num FROM alembic_version", env=env)
        ).decode()
        assert revision.strip() == result["manifest"]["alembic_revision"]
    finally:
        await backup._run("psql", "-qc", f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)', env=admin)


async def test_damaged_archive_and_bad_names(backup_env) -> None:  # type: ignore[no-untyped-def]
    info = await backup.create()
    data = bytearray(info.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    damaged = backup_env / "damaged.pbak"
    damaged.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError):
        await backup.verify(damaged)
    for name in ["../etc/passwd", "paurubot-1.pbak", "x.pbak"]:
        with pytest.raises(backup.BackupError):
            backup.resolve(name)
    assert backup.resolve(info.name) == info.path


async def test_prune_keeps_the_newest(backup_env) -> None:  # type: ignore[no-untyped-def]
    names = []
    for _ in range(3):
        names.append((await backup.create()).name)
        await asyncio.sleep(1.05)
    removed = backup.prune(2)
    assert removed == [names[0]]
    assert [b.name for b in backup.listing()] == [names[2], names[1]]


async def test_refuses_to_back_up_without_a_passphrase(backup_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "backup_passphrase", SecretStr(""))
    with pytest.raises(backup.BackupError, match="BACKUP_PASSPHRASE"):
        await backup.create()
    assert backup.listing() == []


def test_write_env_master_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=x\nMASTER_KEY=old\nDOMAIN=a.example\n")
    backup.write_env_master_key(env, "new-key")
    assert env.read_text() == "BOT_TOKEN=x\nMASTER_KEY=new-key\nDOMAIN=a.example\n"
    other = tmp_path / "fresh.env"
    backup.write_env_master_key(other, "k")
    assert other.read_text() == "MASTER_KEY=k\n"


def test_prune_never_deletes_the_backup_a_rollback_restores(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`pauru update` records its pre-update backup; however few copies are kept, it stays."""
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    names = [f"paurubot-20261105-0{n}0000.pbak" for n in range(5)]
    for age, name in enumerate(reversed(names)):
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (1_800_000_000 - age * 3600, 1_800_000_000 - age * 3600))
    monkeypatch.setattr(settings, "paurubot_previous_backup", str(tmp_path / names[0]))
    removed = backup.prune(2)
    assert sorted(removed) == sorted(names[1:3])
    assert sorted(b.name for b in backup.listing()) == sorted([names[0], names[3], names[4]])
    monkeypatch.setattr(settings, "paurubot_previous_backup", "")
    assert backup.prune(2) == [names[0]]
