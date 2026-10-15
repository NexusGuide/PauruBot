"""F-02 contract tests; unit fakes never create orders or charge a wallet."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from paurubot.api import webapp_auth
from paurubot.api.routes import webapp_auto_renew as route
from paurubot.db.models import ServiceStatus


class Savepoint:
    def __init__(self, service):  # type: ignore[no-untyped-def]
        self.service, self.before = service, deepcopy(service.auto_renew)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        if exc_type:
            self.service.auto_renew = self.before


@pytest.fixture
def auto_api(monkeypatch):  # type: ignore[no-untyped-def]
    service = SimpleNamespace(id=19, auto_renew=None, status=ServiceStatus.ACTIVE, is_trial=False)
    session = SimpleNamespace(begin_nested=lambda: Savepoint(service), flush=AsyncMock(), commit=AsyncMock())
    ctx = SimpleNamespace(
        session=session, customer=SimpleNamespace(id=7), shop=SimpleNamespace(id=9), real_view=route._view
    )
    owned = AsyncMock(return_value=service)

    async def disable(db, row):  # type: ignore[no-untyped-def]
        row.auto_renew = None

    async def enable(db, row, *, plan_id, gb, days):  # type: ignore[no-untyped-def]
        if plan_id == 999:
            raise route.orders.OrderError("plan_unavailable")
        row.auto_renew = (
            {"mode": "renew", "plan_id": plan_id}
            if plan_id
            else {"mode": "extra", **({"gb": gb} if gb else {}), **({"days": days} if days else {})}
        )

    async def view(context, row):  # type: ignore[no-untyped-def]
        return route.preference_view(row)

    audit = AsyncMock()
    monkeypatch.setattr(route, "_owned", owned)
    monkeypatch.setattr(route, "_view", view)
    monkeypatch.setattr(route, "record", audit)
    monkeypatch.setattr(route.auto_renew, "disable", AsyncMock(side_effect=disable))
    monkeypatch.setattr(route.auto_renew, "enable", AsyncMock(side_effect=enable))
    app = FastAPI()
    app.include_router(route.router, prefix="/api/webapp")
    app.dependency_overrides[webapp_auth.webapp_context] = lambda: ctx
    return app, service, ctx, owned, audit


def body(service, **extra):  # type: ignore[no-untyped-def]
    return {
        "mode": "renew",
        "plan_id": 3,
        "expected_revision": route.revision(service),
        "acknowledge_wallet": True,
        **extra,
    }


async def post(app, value):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/webapp/services/19/auto-renew", json=value)


async def test_read_is_not_a_write(auto_api):  # type: ignore[no-untyped-def]
    app, service, ctx, owned, audit = auto_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/webapp/services/19/auto-renew")
    assert response.status_code == 200 and response.json()["status"] == "off"
    owned.assert_awaited_once_with(ctx, 19)
    route.auto_renew.disable.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "extra", "plan_id": 0, "gb": 5, "days": 7},
        {"mode": "off", "plan_id": 0},
        {},
    ],
)
async def test_whole_rule_saved_under_lock_and_audited(auto_api, extra):  # type: ignore[no-untyped-def]
    app, service, ctx, owned, audit = auto_api
    service.auto_renew = {"mode": "extra", "gb": 10, "days": 30}
    response = await post(app, body(service, **extra))
    assert response.status_code == 200
    owned.assert_awaited_once_with(ctx, 19, lock=True)
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["shop_id"] == 9
    assert audit.await_args.kwargs["actor"].id == 7
    expected = (
        None
        if extra.get("mode") == "off"
        else {"mode": "extra", "gb": 5, "days": 7}
        if extra.get("mode") == "extra"
        else {"mode": "renew", "plan_id": 3}
    )
    assert service.auto_renew == expected


async def test_removed_extra_part_is_not_silently_retained(auto_api):  # type: ignore[no-untyped-def]
    app, service, *_ = auto_api
    service.auto_renew = {"mode": "extra", "gb": 5, "days": 30}
    response = await post(app, body(service, mode="extra", plan_id=0, gb=10, days=0))
    assert response.status_code == 200 and service.auto_renew == {"mode": "extra", "gb": 10}


async def test_revision_conflict_has_no_effect(auto_api):  # type: ignore[no-untyped-def]
    app, service, _, _, audit = auto_api
    value = body(service)
    service.auto_renew = {"mode": "renew", "plan_id": 8}
    response = await post(app, value)
    assert response.status_code == 409
    route.auto_renew.disable.assert_not_awaited()
    audit.assert_not_awaited()


async def test_validation_failure_restores_previous_rule(auto_api):  # type: ignore[no-untyped-def]
    app, service, _, _, audit = auto_api
    service.auto_renew = {"mode": "extra", "gb": 10}
    response = await post(app, body(service, plan_id=999))
    assert response.status_code == 422 and service.auto_renew == {"mode": "extra", "gb": 10}
    audit.assert_not_awaited()


async def test_same_active_setting_preserves_worker_metadata(auto_api):  # type: ignore[no-untyped-def]
    app, service, _, _, audit = auto_api
    original = {
        "mode": "renew",
        "plan_id": 3,
        "last_at": "2026-10-14T00:00Z",
        "last_order_id": 99,
        "short": True,
    }
    service.auto_renew = original.copy()
    response = await post(app, body(service))
    assert response.status_code == 200 and service.auto_renew == original
    route.auto_renew.disable.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    "extra",
    [
        {"acknowledge_wallet": False},
        {"acknowledge_wallet": "true"},
        {"acknowledge_wallet": 1},
        {"mode": "future"},
        {"plan_id": "3"},
        {"plan_id": True},
        {"gb": -1},
        {"days": 3651},
        {"customer_id": 7},
        {"shop_id": 9},
        {"last_order_id": 99},
        {"expected_revision": "bad"},
        {"mode": "off"},
        {"gb": 5},
        {"mode": "extra", "plan_id": 0},
    ],
)
async def test_strict_input_rejected_without_mutation(auto_api, extra):  # type: ignore[no-untyped-def]
    app, service, _, _, audit = auto_api
    response = await post(app, body(service, **extra))
    assert response.status_code == 422
    route.auto_renew.disable.assert_not_awaited()
    audit.assert_not_awaited()


async def test_trial_cannot_enable_but_can_disable(auto_api):  # type: ignore[no-untyped-def]
    app, service, *_ = auto_api
    service.is_trial = True
    assert (await post(app, body(service))).status_code == 422
    service.auto_renew = {"mode": "renew", "plan_id": 3}
    assert (await post(app, body(service, mode="off", plan_id=0))).status_code == 200


async def test_not_authenticated_or_not_owned_cannot_mutate(auto_api):  # type: ignore[no-untyped-def]
    app, service, _, owned, audit = auto_api
    owned.side_effect = HTTPException(status_code=404, detail="service_not_found")
    assert (await post(app, body(service))).status_code == 404

    async def denied():
        raise HTTPException(status_code=401)

    app.dependency_overrides[webapp_auth.webapp_context] = denied
    assert (await post(app, body(service))).status_code == 401
    audit.assert_not_awaited()
    route.auto_renew.disable.assert_not_awaited()


@pytest.mark.security
async def test_owned_query_enforces_customer_shop_and_deleted_boundary(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Service
    from tests import factories

    shop = await factories.shop(session)
    other = await factories.shop(session)
    customer = await factories.customer(session, shop)
    stranger = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    row = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="auto-ui-owned",
        status=ServiceStatus.ACTIVE,
    )
    session.add(row)
    await session.flush()
    ctx = SimpleNamespace(session=session, shop=shop, customer=customer)
    assert (await route._owned(ctx, row.id, lock=True)).id == row.id
    for changed in [
        SimpleNamespace(session=session, shop=other, customer=customer),
        SimpleNamespace(session=session, shop=shop, customer=stranger),
    ]:
        with pytest.raises(HTTPException) as exc:
            await route._owned(changed, row.id)
        assert exc.value.status_code == 404
    row.status = ServiceStatus.DELETED
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await route._owned(ctx, row.id)
    assert exc.value.status_code == 404


def test_projection_does_not_expose_worker_fields_or_crash_on_bad_extra() -> None:
    service = SimpleNamespace(
        id=19,
        auto_renew={"mode": "extra", "gb": "bad", "days": 7, "last_order_id": 9, "stopped": "order_failed"},
    )
    view = route.preference_view(service)
    assert view["status"] == "stopped" and view["rule"] == {"mode": "extra", "gb": 0, "days": 7}
    assert "last_order_id" not in view
    first = view["revision"]
    service.auto_renew["last_order_id"] = 10
    assert route.revision(service) != first


async def test_preview_uses_real_draft_validation_but_no_order_or_wallet_charge(auto_api, monkeypatch):  # type: ignore[no-untyped-def]
    _, service, ctx, *_ = auto_api
    from paurubot.api.routes import webapp_auto_renew

    # Restore the production preview function; the fixture only mocks it for mutation-unit tests.
    assert webapp_auto_renew is route
    # Original function stored by fixture before monkeypatching.
    real_view = ctx.real_view
    ctx.shop.currency = "IRT"
    service.panel_id = 4
    offered = [
        SimpleNamespace(id=3, title="Monthly", renewable=True, delivery="subscription"),
        SimpleNamespace(id=4, renewable=False),
    ]
    monkeypatch.setattr(route.catalog, "plans", AsyncMock(return_value=offered))

    async def draft(db, customer, kind, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("extra_days"):
            raise route.orders.OrderError("extras_unavailable")
        return SimpleNamespace(base_price=100)

    monkeypatch.setattr(route.orders, "draft", AsyncMock(side_effect=draft))
    monkeypatch.setattr(route, "wallet_balance", AsyncMock(return_value=50))
    result = await real_view(ctx, service)
    assert result["plans"] == [{"id": 3, "title": "Monthly", "base_price": 100}]
    assert result["day_options"] == [] and len(result["gb_options"]) == 4
    assert result["balance"] == 50
    route.auto_renew.enable.assert_not_awaited()


async def test_change_preserves_cooldown_and_restart_clears_failed_order_marker(auto_api):  # type: ignore[no-untyped-def]
    app, service, *_ = auto_api
    service.auto_renew = {"mode": "renew", "plan_id": 8, "last_at": "2026-10-14T00:00Z", "last_order_id": 99}
    assert (await post(app, body(service))).status_code == 200
    assert service.auto_renew["last_at"] == "2026-10-14T00:00Z" and service.auto_renew["last_order_id"] == 99
    service.auto_renew = {**service.auto_renew, "stopped": "order_failed"}
    assert (await post(app, body(service))).status_code == 200
    assert service.auto_renew["last_at"] == "2026-10-14T00:00Z"
    assert "last_order_id" not in service.auto_renew and "stopped" not in service.auto_renew


async def test_commit_failure_never_returns_confirmed_success(auto_api):  # type: ignore[no-untyped-def]
    _, service, ctx, *_ = auto_api
    ctx.session.commit.side_effect = RuntimeError("commit_failed")
    with pytest.raises(RuntimeError, match="commit_failed"):
        await route.set_auto_renew(19, route.AutoRenewBody(**body(service)), ctx)
