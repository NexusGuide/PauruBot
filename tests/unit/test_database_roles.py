"""SEC-F26: the app runs as a data-only role; the schema owner migrates and restores.

Before, the api, bot and worker connected as the database owner (in the bundled Compose the
superuser): a bug or an injection in the app could drop tables, disable the ledger's
immutability triggers or rewrite the migration record. These tests walk a real fresh database
through migrate, use, backup and restore.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from paurubot.config import get_settings
from paurubot.db import roles
from paurubot.db.migrate import upgrade_database

HOST = "127.0.0.1:5432"


def _url(user: str, password: str, database: str) -> str:
    return f"postgresql+asyncpg://{user}:{password}@{HOST}/{database}"


@pytest.fixture
async def fresh(engine, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """A new database owned by a new owner role, as the bundled Compose sets it up."""
    tag = secrets.token_hex(3)
    owner, app, database = f"own_{tag}", f"app_{tag}", f"roles_{tag}"
    owner_pw, app_pw = secrets.token_hex(8), secrets.token_hex(8)
    async with engine.connect() as admin:
        await admin.execution_options(isolation_level="AUTOCOMMIT")
        await admin.exec_driver_sql(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{owner_pw}'")
        await admin.exec_driver_sql(f"CREATE DATABASE {database} OWNER {owner}")
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", _url(app, app_pw, database))
    monkeypatch.setattr(settings, "database_owner_url", _url(owner, owner_pw, database))
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    yield {"owner": owner, "app": app, "database": database, "app_url": _url(app, app_pw, database)}
    async with engine.connect() as admin:
        await admin.execution_options(isolation_level="AUTOCOMMIT")
        await admin.exec_driver_sql(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        await admin.exec_driver_sql(f"DROP ROLE IF EXISTS {app}")
        await admin.exec_driver_sql(f"DROP ROLE IF EXISTS {owner}")


async def _refused(conn, sql: str) -> bool:  # type: ignore[no-untyped-def]
    try:
        async with conn.begin_nested():
            await conn.exec_driver_sql(sql)
    except DBAPIError as exc:
        return any(word in str(exc) for word in ("permission denied", "must be owner", "must be superuser"))
    return False


async def test_migrate_gives_the_app_data_rights_only(fresh) -> None:  # type: ignore[no-untyped-def]
    assert await upgrade_database() == fresh["app"]
    assert await upgrade_database() == fresh["app"]  # every migrate run re-applies, idempotently
    app = create_async_engine(fresh["app_url"])
    try:
        async with app.connect() as conn:
            async with conn.begin():
                await conn.execute(
                    text(
                        "INSERT INTO shops (title, status, default_locale, currency, created_at, updated_at) "
                        "VALUES ('Roles', 'active', 'fa', 'IRT', now(), now())"
                    )
                )
                assert await conn.scalar(text("SELECT count(*) FROM shops")) == 1
                assert await conn.scalar(
                    text("SELECT version_num FROM alembic_version")
                )  # readiness reads it
            async with conn.begin():
                for sql in (
                    "CREATE TABLE intruder (id int)",
                    "DROP TABLE services",
                    "ALTER TABLE ledger_entries DISABLE TRIGGER ALL",
                    "TRUNCATE ledger_entries",
                    "UPDATE alembic_version SET version_num = 'forged'",
                    "CREATE ROLE intruder",
                ):
                    assert await _refused(conn, sql), sql
            assert await roles.runtime_problems(conn) == []
    finally:
        await app.dispose()


async def test_backup_as_the_app_and_restore_as_the_owner(fresh, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.ops import backup

    monkeypatch.setattr(
        get_settings(), "backup_passphrase", type(get_settings().backup_passphrase)("pp-" * 8)
    )
    await upgrade_database()
    app = create_async_engine(fresh["app_url"])
    try:
        async with app.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO shops (title, status, default_locale, currency, created_at, updated_at) "
                    "VALUES ('Before', 'active', 'fa', 'IRT', now(), now())"
                )
            )
        info = await backup.create(label="roles")  # pg_dump with the app's SELECT rights
        async with app.begin() as conn:
            await conn.execute(text("UPDATE shops SET title = 'After'"))
        await app.dispose()  # restore replaces the tables; drop pooled connections first
        await backup.restore(Path(info.path))
        app = create_async_engine(fresh["app_url"])
        async with app.connect() as conn:
            # Tables recreated by the owner are readable again through its default privileges.
            assert await conn.scalar(text("SELECT title FROM shops")) == "Before"
    finally:
        await app.dispose()


async def test_one_role_setups_keep_working(engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "database_owner_url", "")
    assert get_settings().owner_database_url == get_settings().database_url
    async with engine.connect() as conn:
        assert await roles.ensure_runtime_role(conn, str(engine.url)) is None
        assert "is a superuser" in await roles.runtime_problems(conn)


def test_deployment_gives_the_owner_only_to_migrate_and_restore() -> None:
    import yaml

    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    services = compose["services"]
    for name in ("api", "bot", "worker"):
        env = services[name]["environment"]
        assert "DATABASE_OWNER_URL" not in env and "POSTGRES_APP_USER" in env["DATABASE_URL"], name
    assert "POSTGRES_USER" in services["migrate"]["environment"]["DATABASE_OWNER_URL"]
    # AUD-2: env_file loads all of .env, the owner's password too. The app containers get it
    # blanked: compose reads it to build the migrate URL, no running process needs it.
    for name in ("api", "bot", "worker", "migrate"):
        assert services[name].get("env_file") == ".env", name
        assert services[name]["environment"].get("POSTGRES_PASSWORD") == "", name
    installer = (root / "install.sh").read_text()
    assert "POSTGRES_APP_PASSWORD=$(random_secret" in installer
    assert "migrate restore " in installer and " api restore " not in installer
