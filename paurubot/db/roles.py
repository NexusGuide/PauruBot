"""Least-privilege runtime database role (SEC-F26).

The api, bot and worker connect as a runtime role that may only read and write rows: no DDL,
no TRUNCATE, no superuser, database or role creation, and no writes to `alembic_version`. Migrations,
restore and full backup verification connect as the schema owner (DATABASE_OWNER_URL). Every
`migrate` run applies the grants below again, so they also cover tables a migration or a
restore has just recreated; default privileges cover tables created later by the owner.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

DATA_RIGHTS = "SELECT, INSERT, UPDATE, DELETE"


async def _format(conn: AsyncConnection, template: str, *values: str) -> str:
    """Quote identifiers (%I) and literals (%L) on the server, never in Python."""
    params = {f"v{i}": value for i, value in enumerate(values)}
    # format() is variadic: the driver cannot infer the parameter types without the casts.
    args = "".join(f", CAST(:v{i} AS text)" for i in range(len(values)))
    statement = await conn.scalar(text(f"SELECT format('{template}'{args})"), params)
    assert isinstance(statement, str)
    return statement


async def _run(conn: AsyncConnection, template: str, *values: str) -> None:
    # exec_driver_sql: the formatted statement is never parsed for bind parameters again.
    await conn.exec_driver_sql(await _format(conn, template, *values))


class RoleError(RuntimeError):
    """The runtime role has rights the owner cannot take away; an administrator must."""


# Attribute -> the value the runtime role must have.
_ATTRIBUTES = {
    "rolcanlogin": ("LOGIN", True),
    "rolsuper": ("SUPERUSER", False),
    "rolcreatedb": ("CREATEDB", False),
    "rolcreaterole": ("CREATEROLE", False),
    "rolreplication": ("REPLICATION", False),
    "rolbypassrls": ("BYPASSRLS", False),
}


async def _restrict(conn: AsyncConnection, role: str) -> None:
    """Change only what differs: a non-superuser owner may not even mention SUPERUSER and its peers."""
    columns = ", ".join(_ATTRIBUTES)
    row = (await conn.execute(text(f"SELECT {columns} FROM pg_roles WHERE rolname = :r"), {"r": role})).one()
    changes = [
        (word if wanted else f"NO{word}")
        for (word, wanted), value in zip(_ATTRIBUTES.values(), row, strict=True)
        if bool(value) != wanted
    ]
    if not changes:
        return
    try:
        async with conn.begin_nested():
            await _run(conn, "ALTER ROLE %I WITH " + " ".join(changes), role)
    except Exception as exc:
        raise RoleError(
            f"runtime role {role} needs {' '.join(changes)}; the schema owner may not change that"
        ) from exc


async def ensure_runtime_role(conn: AsyncConnection, runtime_url: str) -> str | None:
    """Create or align the runtime role of `runtime_url`. Returns its name, or None for one-role setups.

    `conn` must belong to the schema owner, with the right to create roles.
    """
    runtime = make_url(runtime_url)
    owner = await conn.scalar(text("SELECT current_user"))
    database = await conn.scalar(text("SELECT current_database()"))
    role = runtime.username
    if not role or role == owner or not isinstance(owner, str) or not isinstance(database, str):
        return None
    password = runtime.password if isinstance(runtime.password, str) else None
    exists = await conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role})
    if not exists:
        await _run(conn, "CREATE ROLE %I LOGIN", role)
    if password:
        await _run(conn, "ALTER ROLE %I WITH PASSWORD %L", role, password)
    await _restrict(conn, role)
    await _run(conn, "GRANT CONNECT ON DATABASE %I TO %I", database, role)
    await conn.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    await _run(conn, "GRANT USAGE ON SCHEMA public TO %I", role)
    await _run(conn, f"GRANT {DATA_RIGHTS} ON ALL TABLES IN SCHEMA public TO %I", role)
    await _run(conn, "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I", role)
    await _run(
        conn,
        f"ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT {DATA_RIGHTS} ON TABLES TO %I",
        owner,
        role,
    )
    await _run(
        conn,
        "ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I",
        owner,
        role,
    )
    # The app reads the schema revision (readiness) but must never move it.
    has_version = await conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
    if has_version:
        await _run(conn, "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.alembic_version FROM %I", role)
    return role


async def runtime_problems(conn: AsyncConnection) -> list[str]:
    """What is wrong with the role this connection runs as, for the doctor."""
    row = (
        await conn.execute(
            text(
                "SELECT r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
                "has_schema_privilege(current_user, 'public', 'CREATE'), "
                "EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user) "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        )
    ).one()
    problems = []
    if row[0]:
        problems.append("is a superuser")
    if row[1] or row[2]:
        problems.append("may create databases or roles")
    if row[3]:
        problems.append("may create tables")
    if row[4]:
        problems.append("owns tables")
    return problems
