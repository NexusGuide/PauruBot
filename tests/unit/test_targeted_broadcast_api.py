"""Additive F-15/F-16 contract: no real campaign, DB or Telegram delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from paurubot.api import deps
from paurubot.api.routes.admin import growth

KINDS = ("all", "buyers", "resellers", "active", "inactive", "never_bought")


@pytest.fixture
def targeted_api(monkeypatch):  # type: ignore[no-untyped-def]
    ctx = SimpleNamespace(shop_id=19, can=lambda permission: permission == "broadcast.send")
    db = object()
    create = AsyncMock(return_value={"accepted": True})
    count = AsyncMock(return_value={"reachable": 7})
    monkeypatch.setattr(growth, "create_broadcast", create)
    monkeypatch.setattr(growth, "audience_size", count)
    app = FastAPI()
    app.include_router(growth.router)
    app.dependency_overrides[deps.current_staff] = lambda: ctx
    app.dependency_overrides[deps.get_db] = lambda: db
    return app, ctx, db, create, count


@pytest.mark.parametrize("kind", KINDS)
async def test_targeted_routes_delegate_same_kind_locale_and_tenant(targeted_api, kind):  # type: ignore[no-untyped-def]
    app, ctx, db, create, count = targeted_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/broadcasts/targeted",
            json={"text": "literal <b>text</b>", "kind": kind, "locale": "fa"},
        )
        assert response.status_code == 200
        body, actual_ctx, actual_db = create.await_args.args
        assert body.kind == kind and body.locale == "fa"
        assert actual_ctx is ctx and actual_ctx.shop_id == 19 and actual_db is db
        response = await client.get(
            "/api/admin/broadcasts/targeted/audience",
            params={"kind": kind, "locale": "fa"},
        )
        assert response.json() == {"reachable": 7}
        count.assert_awaited_once_with(ctx, db, kind=kind, locale="fa")


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"kind": "future"},
        {"kind": None},
        {"kind": "active", "buyers_only": True},
        {"kind": "all", "resellers_only": True},
        {"kind": "buyers", "resellers_only": True, "buyers_only": True},
        {"kind": "active", "buyers_only": "false"},
        {"kind": "all", "locale": "x" * 9},
    ],
)
async def test_invalid_targeted_create_does_not_mutate(targeted_api, extra):  # type: ignore[no-untyped-def]
    app, _, _, create, _ = targeted_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/admin/broadcasts/targeted", json={"text": "x", **extra})
    assert response.status_code == 422
    create.assert_not_awaited()


@pytest.mark.parametrize("params", [{}, {"kind": "future"}, {"kind": "all", "locale": "x" * 9}])
async def test_invalid_preview_is_not_unfiltered_count(targeted_api, params):  # type: ignore[no-untyped-def]
    app, _, _, _, count = targeted_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/admin/broadcasts/targeted/audience", params=params)
    assert response.status_code == 422
    count.assert_not_awaited()


async def test_permission_denied_before_count_or_create(targeted_api):  # type: ignore[no-untyped-def]
    app, ctx, _, create, count = targeted_api
    ctx.can = lambda permission: False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/admin/broadcasts/targeted/audience?kind=all")).status_code == 403
        assert (
            await client.post("/api/admin/broadcasts/targeted", json={"text": "x", "kind": "all"})
        ).status_code == 403
    count.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize("kind,flag", [("buyers", "buyers_only"), ("resellers", "resellers_only")])
async def test_matching_legacy_mirror_is_allowed(targeted_api, kind, flag):  # type: ignore[no-untyped-def]
    app, _, _, create, _ = targeted_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/admin/broadcasts/targeted",
            json={"text": "x", "kind": kind, flag: True},
        )
    assert response.status_code == 200
    create.assert_awaited_once()


def test_legacy_body_still_defaults_to_all() -> None:
    assert growth.BroadcastBody(text="x").kind == "all"
