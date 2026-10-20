"""Real PostgreSQL inbox persistence, scope, consent filtering and adapter guards."""

from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from paurubot.api.deps import current_staff, get_db
from paurubot.api.routes import announcements as routes
from paurubot.api.webapp_auth import WebAppContext, webapp_context
from paurubot.audit.log import Actor
from paurubot.core import announcements as core
from paurubot.db.base import Base, utcnow
from paurubot.db.models import AuditLog, Customer, Shop
from paurubot.db.models.announcements import Announcement, AnnouncementRead

pytestmark = pytest.mark.security
MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0026_announcements.py"


def publication(**changes):  # type: ignore[no-untyped-def]
    return core.Publish(
        request_key=uuid4(),
        title="Notice",
        text="Plain <b>text</b>",
        kind="operational",
        **changes,
    )


async def seed(session):  # type: ignore[no-untyped-def]
    shops = [Shop(title="First"), Shop(title="Second")]
    session.add_all(shops)
    await session.flush()
    users = [
        Customer(shop_id=shops[0].id, telegram_id=10),
        Customer(shop_id=shops[0].id, telegram_id=20),
        Customer(shop_id=shops[1].id, telegram_id=30),
    ]
    session.add_all(users)
    await session.flush()
    return shops, users


async def test_publication_replay_is_immutable_and_audit_once(session):  # type: ignore[no-untyped-def]
    shops, _ = await seed(session)
    body = publication()
    first = await core.publish(session, shops[0].id, Actor("staff"), body)
    assert (await core.publish(session, shops[0].id, Actor("staff"), body)).id == first.id
    with pytest.raises(core.InboxError, match="announcement_conflict"):
        await core.publish(
            session,
            shops[0].id,
            Actor("staff"),
            body.model_copy(update={"text": "Changed"}),
        )
    assert await session.scalar(select(func.count()).select_from(Announcement)) == 1
    assert (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "announcement.publish")
        )
        == 1
    )
    # The same external key is independently scoped to the second shop.
    other = await core.publish(session, shops[1].id, Actor("staff"), body)
    assert other.id != first.id


async def test_persist_seen_read_and_no_cross_customer_state(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    row = await core.publish(session, shops[0].id, Actor("staff"), publication())
    data = await core.inbox(session, shops[0].id, users[0].id)
    assert data["items"][0]["seen_at"] is None
    assert (
        await session.scalar(select(func.count()).select_from(AnnouncementRead)) == 0
    )  # GET is side-effect free
    first = await core.mark(session, shops[0].id, users[0].id, row.id, "seen")
    assert first["seen_at"] and first["read_at"] is None
    read = await core.mark(session, shops[0].id, users[0].id, row.id, "read")
    again = await core.mark(session, shops[0].id, users[0].id, row.id, "seen")
    assert again["seen_at"] == first["seen_at"] and again["read_at"] == read["read_at"]
    assert (await core.inbox(session, shops[0].id, users[0].id, unread=True))["items"] == []
    assert (await core.inbox(session, shops[0].id, users[1].id))["items"][0]["read_at"] is None
    await session.flush()
    shop_id, customer_id = shops[0].id, users[0].id
    session.expire_all()
    assert (await core.inbox(session, shop_id, customer_id))["items"][0]["read_at"] == read["read_at"]


async def test_shop_scope_blocks_content_and_known_id_writes(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    row = await core.publish(session, shops[0].id, Actor("staff"), publication())
    assert (await core.inbox(session, shops[1].id, users[2].id))["items"] == []
    with pytest.raises(core.InboxError, match="announcement_not_found"):
        await core.mark(session, shops[1].id, users[2].id, row.id, "read")
    with pytest.raises(core.InboxError):
        await core.inbox(session, shops[1].id, users[0].id)
    with pytest.raises(core.InboxError):
        await core.revoke(session, shops[1].id, Actor("staff"), row.id)
    assert await session.scalar(select(func.count()).select_from(AnnouncementRead)) == 0


async def test_schedule_expiry_revoke_and_bans(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    now = utcnow()
    future = await core.publish(
        session,
        shops[0].id,
        Actor("staff"),
        publication(starts_at=now + timedelta(days=1)),
    )
    expired = await core.publish(
        session,
        shops[0].id,
        Actor("staff"),
        publication(starts_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)),
    )
    active = await core.publish(session, shops[0].id, Actor("staff"), publication())
    for row in (future, expired):
        with pytest.raises(core.InboxError):
            await core.mark(session, shops[0].id, users[0].id, row.id, "seen")
    assert [x["id"] for x in (await core.inbox(session, shops[0].id, users[0].id))["items"]] == [active.id]
    await core.revoke(session, shops[0].id, Actor("staff"), active.id)
    await core.revoke(session, shops[0].id, Actor("staff"), active.id)
    assert (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "announcement.revoke")
        )
        == 1
    )
    assert (await core.inbox(session, shops[0].id, users[0].id))["items"] == []
    with pytest.raises(core.InboxError):
        await core.mark(session, shops[0].id, users[0].id, active.id, "read")
    users[0].is_banned = True
    await session.flush()
    with pytest.raises(core.InboxError):
        await core.inbox(session, shops[0].id, users[0].id)


async def test_current_opt_out_filters_marketing_but_not_operational(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    op = await core.publish(session, shops[0].id, Actor("staff"), publication())
    ad = await core.publish(
        session,
        shops[0].id,
        Actor("staff"),
        publication().model_copy(update={"kind": "marketing"}),
    )
    users[0].marketing_opt_out = True
    await session.flush()
    assert [x["id"] for x in (await core.inbox(session, shops[0].id, users[0].id))["items"]] == [op.id]
    with pytest.raises(core.InboxError):
        await core.mark(session, shops[0].id, users[0].id, ad.id, "read")
    assert len((await core.inbox(session, shops[0].id, users[1].id))["items"]) == 2
    users[0].marketing_opt_out = False
    await session.flush()
    assert len((await core.inbox(session, shops[0].id, users[0].id))["items"]) == 2


async def test_keyset_pages_apply_unread_before_limit(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    rows = [await core.publish(session, shops[0].id, Actor("staff"), publication()) for _ in range(7)]
    for row in rows[2:]:
        await core.mark(session, shops[0].id, users[0].id, row.id, "read")
    first = await core.inbox(session, shops[0].id, users[0].id, unread=True, limit=1)
    assert first["items"][0]["id"] == rows[1].id
    second = await core.inbox(
        session,
        shops[0].id,
        users[0].id,
        unread=True,
        limit=1,
        before=first["next_before"],
    )
    assert second["items"][0]["id"] == rows[0].id and second["next_before"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": "x" * 129},
        {"text": "x" * 4097},
        {"text": "bad\x00text"},
        {"kind": "finance"},
        {"request_key": "bad"},
        {"shop_id": 9},
        {"customer_id": 99},
        {"starts_at": "2026-01-01T00:00:00"},
        {"starts_at": "2026-01-02T00:00:00Z", "expires_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_reject_invalid_input(changes):  # type: ignore[no-untyped-def]
    value = publication().model_dump()
    value.update(changes)
    with pytest.raises(ValidationError):
        core.Publish.model_validate(value)


async def test_expired_publication_is_still_replayable(session):  # type: ignore[no-untyped-def]
    shops, _ = await seed(session)
    body = publication(expires_at=utcnow() + timedelta(seconds=60))
    row = await core.publish(session, shops[0].id, Actor("staff"), body)
    # Simulate retry well after that publication's window without changing its immutable fingerprint.
    row.starts_at = utcnow() - timedelta(days=2)
    row.expires_at = utcnow() - timedelta(days=1)
    await session.flush()
    assert (await core.publish(session, shops[0].id, Actor("staff"), body)).id == row.id


async def client_for(session, shop, user, *, allowed=True, scoped=False):  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(routes.router)

    async def db():
        yield session

    ctx = SimpleNamespace(shop_id=shop.id, actor=Actor("staff"), scoped=scoped, can=lambda p: allowed)

    async def staff():
        return ctx

    async def webapp():
        return WebAppContext(session, shop, user, {})

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[current_staff] = staff
    app.dependency_overrides[webapp_context] = webapp
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.invalid")


@pytest.mark.parametrize("allowed,scoped", [(False, False), (True, True)])
async def test_permission_or_reseller_scope_cannot_publish(session, allowed, scoped):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    async with await client_for(session, shops[0], users[0], allowed=allowed, scoped=scoped) as client:
        assert (await client.get("/api/admin/announcements")).status_code == 403
        assert (
            await client.post("/api/admin/announcements", json=publication().model_dump(mode="json"))
        ).status_code == 403
        assert (await client.post("/api/admin/announcements/1/revoke")).status_code == 403
    assert await session.scalar(select(func.count()).select_from(Announcement)) == 0


async def test_routes_publish_read_revoke_and_strict_contract(session):  # type: ignore[no-untyped-def]
    shops, users = await seed(session)
    async with await client_for(session, shops[0], users[0]) as client:
        response = await client.post("/api/admin/announcements", json=publication().model_dump(mode="json"))
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        row_id = response.json()["item"]["id"]
        page = await client.get("/api/webapp/announcements")
        assert page.headers["cache-control"] == "no-store" and len(page.json()["items"]) == 1
        assert (
            await client.post(
                f"/api/webapp/announcements/{row_id}/state",
                json={"action": "read", "customer_id": users[1].id},
            )
        ).status_code == 422
        assert (
            await client.post(f"/api/webapp/announcements/{row_id}/state", json={"action": "read"})
        ).json()["item"]["read_at"]
        assert (await client.get("/api/webapp/announcements?unread=true")).json()["items"] == []
        assert (await client.get("/api/webapp/announcements?limit=51")).status_code == 422
        assert (
            await client.get("/api/webapp/announcements?before=99999999999999999999999")
        ).status_code == 422
        assert (
            await client.post(
                "/api/webapp/announcements/999999999999999999999/state",
                json={"action": "read"},
            )
        ).status_code == 422
        assert (await client.post(f"/api/admin/announcements/{row_id}/revoke")).status_code == 200
        assert (await client.get("/api/webapp/announcements")).json()["items"] == []
        assert (
            await client.post(f"/api/webapp/announcements/{row_id}/state", json={"action": "seen"})
        ).status_code == 404


async def test_routes_require_auth_without_overrides():
    app = FastAPI()
    app.include_router(routes.router)
    # A malformed/absent initData is denied by the production dependency; don't enter a DB fixture.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
    ) as client:
        response = await client.get("/api/webapp/announcements")
        assert response.status_code in {400, 401, 403, 422}


async def test_concurrent_commits_publish_once_and_preserve_first_read(engine):  # type: ignore[no-untyped-def]
    """Independent DB transactions, not a pair of savepoints on the same connection."""
    schema = "announcements_" + secrets.token_hex(8)
    async with engine.begin() as conn:
        await conn.execute(CreateSchema(schema))
        scoped = await conn.execution_options(schema_translate_map={None: schema})
        await scoped.run_sync(Base.metadata.create_all)
    local = engine.execution_options(schema_translate_map={None: schema})
    maker = async_sessionmaker(local, expire_on_commit=False)
    try:
        async with maker() as session:
            shops, users = await seed(session)
            shop_id, customer_id = shops[0].id, users[0].id
            await session.commit()
        body = publication()

        async def publish():
            async with maker() as session:
                row = await core.publish(session, shop_id, Actor("staff"), body)
                await session.commit()
                return row.id

        ids = await asyncio.gather(publish(), publish())
        assert ids[0] == ids[1]

        async def mark(action):
            async with maker() as session:
                result = await core.mark(session, shop_id, customer_id, ids[0], action)
                await session.commit()
                return result

        await asyncio.gather(mark("read"), mark("seen"), mark("read"))
        async with maker() as session:
            page = await core.inbox(session, shop_id, customer_id)
            state = page["items"][0]
            assert state["seen_at"] and state["read_at"]
            assert await session.scalar(select(func.count()).select_from(AnnouncementRead)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "announcement.publish")
                )
                == 1
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True))


async def test_read_only_api_key_and_cookie_csrf_still_guard_writes(session, monkeypatch):  # type: ignore[no-untyped-def]
    from paurubot.api import deps

    shops, users = await seed(session)
    app = FastAPI()
    app.include_router(routes.router)

    async def db():
        yield session

    app.dependency_overrides[get_db] = db

    async def resolve_key(db, token):
        return SimpleNamespace(), SimpleNamespace(read_only=True)

    monkeypatch.setattr(deps.staff_service, "resolve_api_key", resolve_key, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
    ) as client:
        reply = await client.post(
            "/api/admin/announcements",
            headers={"authorization": "Bearer pbk_synthetic"},
            json=publication().model_dump(mode="json"),
        )
        assert reply.status_code == 403 and "read_only_key" in reply.text

        async def load_session(key):
            return SimpleNamespace(csrf="real-csrf")

        async def resolve_staff(db, data):
            return SimpleNamespace()

        monkeypatch.setattr(deps.sessions, "load", load_session)
        monkeypatch.setattr(deps.staff_service, "resolve", resolve_staff, raising=False)
        reply = await client.post(
            "/api/admin/announcements",
            headers={"cookie": "pb_session=synthetic", "x-csrf-token": "wrong"},
            json=publication().model_dump(mode="json"),
        )
        assert reply.status_code == 403 and "csrf_failed" in reply.text
    assert await session.scalar(select(func.count()).select_from(Announcement)) == 0


async def test_migration_round_trip_in_private_schema(engine):  # type: ignore[no-untyped-def]
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    spec = importlib.util.spec_from_file_location("announcement_migration", MIGRATION_PATH)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "announcement_migration_" + secrets.token_hex(8)

    def round_trip(connection):
        with Operations.context(
            MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
        ):
            migration.upgrade()
            for name in ("announcements", "announcement_reads"):
                actual = {c["name"]: c for c in inspect(connection).get_columns(name, schema=schema)}
                expected = Base.metadata.tables[name]
                assert set(actual) == {column.name for column in expected.columns}
                for column in expected.columns:
                    assert actual[column.name]["nullable"] == column.nullable
            migration.downgrade()
            assert not inspect(connection).has_table("announcements", schema=schema)
            assert not inspect(connection).has_table("announcement_reads", schema=schema)
            migration.upgrade()
            assert inspect(connection).has_table("announcements", schema=schema)

    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            await connection.run_sync(round_trip)
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
