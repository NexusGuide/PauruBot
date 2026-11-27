"""Encrypted backups with a hashed manifest and a validated restore (OPS-05, NEW-01).

An archive holds a PostgreSQL custom-format dump, a manifest of SHA-256 hashes and a copy of
MASTER_KEY. The whole archive is encrypted with BACKUP_PASSPHRASE, so restoring on a fresh server
recovers panel passwords and payment keys too; `.env` itself is never included.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from paurubot import __version__
from paurubot.config import get_settings
from paurubot.ops.crypto import CryptoError, decrypt_file, encrypt_file

FORMAT = 1
SUFFIX = ".pbak"
NAME_RE = re.compile(r"^paurubot-\d{8}-\d{6}(-[a-z0-9]{4})?\.pbak$")
MEMBERS = {"manifest.json", "db.dump", "keys.json"}


class BackupError(Exception):
    pass


@dataclass(slots=True)
class BackupInfo:
    name: str
    path: Path
    size: int
    created_at: datetime


@dataclass(slots=True)
class Contents:
    manifest: dict[str, Any]
    master_key: str
    dump: Path


def backup_dir() -> Path:
    path = Path(get_settings().backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def pg_env(url: str | None = None) -> dict[str, str]:
    """libpq connection settings as environment variables, so the password never hits argv."""
    parsed = make_url(url or get_settings().database_url)
    env = dict(os.environ)
    env.update(
        {
            "PGHOST": parsed.host or "localhost",
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": parsed.username or "postgres",
            "PGDATABASE": parsed.database or "postgres",
        }
    )
    if parsed.password:
        env["PGPASSWORD"] = str(parsed.password)
    return env


async def _run(*args: str, env: dict[str, str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        message = err.decode(errors="replace").strip().splitlines()[-1:] or ["failed"]
        raise BackupError(f"{args[0]}: {message[0][:300]}")
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(master_key: str) -> str:
    return hashlib.sha256(b"paurubot/master-key/" + master_key.encode()).hexdigest()[:16]


async def _current_revision(env: dict[str, str]) -> str | None:
    try:
        out = await _run("psql", "-Atqc", "SELECT version_num FROM alembic_version", env=env)
    except BackupError:
        return None
    return out.decode().strip() or None


async def create(*, label: str = "") -> BackupInfo:
    """Write a new encrypted archive into the backup directory and return it."""
    settings = get_settings()
    passphrase = settings.backup_passphrase.get_secret_value()
    master_key = settings.master_key.get_secret_value()
    if not passphrase:
        raise BackupError("BACKUP_PASSPHRASE is empty; refusing to write an unencrypted backup")
    env = pg_env()
    now = datetime.now(UTC)
    name = f"paurubot-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}{SUFFIX}"
    target = backup_dir() / name
    with tempfile.TemporaryDirectory(prefix=".work-", dir=backup_dir()) as tmp:
        work = Path(tmp)
        dump = work / "db.dump"
        await _run("pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={dump}", env=env)
        server = (await _run("psql", "-Atqc", "SHOW server_version", env=env)).decode().strip()
        manifest = {
            "format": FORMAT,
            "app_version": __version__,
            "created_at": now.isoformat(),
            "label": label[:64],
            "alembic_revision": await _current_revision(env),
            "postgres": server,
            "master_key_fingerprint": _fingerprint(master_key) if master_key else None,
            "files": {"db.dump": {"sha256": _sha256(dump), "size": dump.stat().st_size}},
        }
        (work / "keys.json").write_text(json.dumps({"master_key": master_key}))
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        tar_path = work / "archive.tar"
        with tarfile.open(tar_path, "w") as tar:
            for member in ("manifest.json", "keys.json", "db.dump"):
                tar.add(work / member, arcname=member)
        partial = target.with_suffix(".partial")
        encrypt_file(tar_path, partial, passphrase)
        os.chmod(partial, 0o600)
        partial.replace(target)
    return BackupInfo(name, target, target.stat().st_size, now)


def listing() -> list[BackupInfo]:
    items: list[BackupInfo] = []
    root = Path(get_settings().backup_dir)
    if not root.is_dir():
        return items
    for path in root.glob(f"*{SUFFIX}"):
        if not NAME_RE.match(path.name):
            continue
        stat = path.stat()
        items.append(BackupInfo(path.name, path, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC)))
    return sorted(items, key=lambda info: info.created_at, reverse=True)


def resolve(name: str) -> Path:
    if not NAME_RE.match(name):
        raise BackupError("invalid backup name")
    path = Path(get_settings().backup_dir) / name
    if not path.is_file():
        raise BackupError("backup not found")
    return path


def _protected_backup_names() -> set[str]:
    settings = get_settings()
    return {
        Path(raw).name
        for raw in (settings.paurubot_previous_backup, settings.paurubot_pre_dev_backup)
        if raw
    }


def delete_archive(name: str) -> None:
    """Remove one backup archive unless it is reserved for rollback or dev return."""
    path = resolve(name)
    if path.name in _protected_backup_names():
        raise BackupError("backup_protected")
    path.unlink(missing_ok=True)


def prune(keep: int) -> list[str]:
    """Delete all but the newest `keep`; protected rollback and pre-dev backups stay."""
    protected = _protected_backup_names()
    removed: list[str] = []
    for info in [b for b in listing() if b.name not in protected][max(1, keep) :]:
        info.path.unlink(missing_ok=True)
        removed.append(info.name)
    return removed


def open_archive(path: Path, workdir: Path, passphrase: str | None = None) -> Contents:
    """Decrypt into `workdir`, refuse unexpected members, and check every hash in the manifest."""
    secret = passphrase if passphrase is not None else get_settings().backup_passphrase.get_secret_value()
    tar_path = workdir / "archive.tar"
    try:
        decrypt_file(path, tar_path, secret)
    except CryptoError as exc:
        raise BackupError(str(exc)) from exc
    with tarfile.open(tar_path, "r") as tar:
        members = tar.getmembers()
        names = {m.name for m in members}
        if names != MEMBERS or any(not m.isfile() for m in members):
            raise BackupError("archive contents are not what PauruBot writes")
        for member in members:
            source = tar.extractfile(member)
            assert source is not None
            with (workdir / member.name).open("wb") as out:
                shutil.copyfileobj(source, out)
    tar_path.unlink()
    manifest = json.loads((workdir / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')}")
    for file_name, meta in manifest.get("files", {}).items():
        file_path = workdir / file_name
        if file_name not in MEMBERS or not file_path.is_file():
            raise BackupError(f"manifest lists a missing file {file_name}")
        if file_path.stat().st_size != meta["size"] or _sha256(file_path) != meta["sha256"]:
            raise BackupError(f"{file_name} does not match its hash; the backup is damaged")
    keys = json.loads((workdir / "keys.json").read_text())
    return Contents(manifest, str(keys.get("master_key") or ""), workdir / "db.dump")


async def verify(path: Path, *, passphrase: str | None = None, full: bool = False) -> dict[str, Any]:
    """Prove a backup restores: hashes always, and with `full` a real restore into a scratch database."""
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=backup_dir()) as tmp:
        contents = open_archive(path, Path(tmp), passphrase)
        env = pg_env()
        listing_out = await _run("pg_restore", "--list", str(contents.dump), env=env)
        result: dict[str, Any] = {
            "manifest": contents.manifest,
            "objects": sum(
                1 for line in listing_out.decode().splitlines() if line and not line.startswith(";")
            ),
            "master_key_included": bool(contents.master_key),
        }
        if full:
            # A scratch database needs the owner's rights, not the runtime role's (SEC-F26).
            env = pg_env(get_settings().owner_database_url)
            scratch = f"paurubot_verify_{secrets.token_hex(4)}"
            admin_env = dict(env, PGDATABASE="postgres")
            await _run("psql", "-qc", f'CREATE DATABASE "{scratch}"', env=admin_env)
            try:
                await _run(
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    f"--dbname={scratch}",
                    str(contents.dump),
                    env=env,
                )
                counts = await _run(
                    "psql",
                    "-Atqc",
                    "SELECT (SELECT count(*) FROM customers), (SELECT count(*) FROM services), "
                    "(SELECT count(*) FROM ledger_entries)",
                    env=dict(env, PGDATABASE=scratch),
                )
                customers, services, entries = (int(x) for x in counts.decode().strip().split("|"))
                result["restored"] = {"customers": customers, "services": services, "ledger_entries": entries}
            finally:
                await _run("psql", "-qc", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)', env=admin_env)
        return result


async def restore(path: Path, *, passphrase: str | None = None) -> Contents:
    """Replace the current database with the backup. Callers stop the other processes first."""
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=backup_dir()) as tmp:
        contents = open_archive(path, Path(tmp), passphrase)
        # Restore drops and recreates objects: the schema owner does it (SEC-F26).
        env = pg_env(get_settings().owner_database_url)
        await _run(
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "--exit-on-error",
            f"--dbname={env['PGDATABASE']}",
            str(contents.dump),
            env=env,
        )
        return Contents(contents.manifest, contents.master_key, Path("db.dump"))


def master_key_matches(contents: Contents) -> bool:
    return contents.master_key == get_settings().master_key.get_secret_value()


def write_env_master_key(env_path: Path, master_key: str) -> None:
    """Set MASTER_KEY in an .env file in place, keeping every other line."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith("MASTER_KEY="):
            out.append(f"MASTER_KEY={master_key}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"MASTER_KEY={master_key}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(env_path)
