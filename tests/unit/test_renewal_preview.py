"""Authenticated renewal forecasts agree with worker policy without reserving a target."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, Order, OrderKind, OrderStatus, Payment, Service, ServiceStatus
from paurubot.panels import GB
from paurubot.provisioning import lifecycle, renewal_policy
from tests import factories
from tests.unit.test_webapp_api import app_env as app_env
from tests.unit.test_webapp_api import h


@pytest.fixture
async def renewal_env(session, app_env, monkeypatch):  # type: ignore[no-untyped-def]
    client, shop, plan, panel = (app_env[k] for k in ("client", "shop", "plan", "panel"))
    owner = await factories.customer(session, shop, telegram_id=89201)
    plan.days, plan.data_gb = 30, 20
    service = Service(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="renewal-preview-owned",
        status=ServiceStatus.ACTIVE,
        expire_at=utcnow() + timedelta(days=10),
        data_limit=10 * GB,
        used_bytes=3 * GB,
    )
    session.add(service)
    await session.flush()
    order = Order(
        shop_id=shop.id,
        customer_id=owner.id,
        kind=OrderKind.RENEW,
        service_id=service.id,
        plan_id=plan.id,
        amount=90_000,
        expires_at=utcnow() + timedelta(minutes=10),
        params={"private_test_marker": "never exposed"},
    )
    session.add(order)
    await shop_settings.set_many(session, shop.id, {"renew_queue_when_active": False})
    await session.commit()
    no_io = AsyncMock(side_effect=AssertionError("preview must not contact a panel or apply a change"))
    monkeypatch.setattr(lifecycle, "driver_for", no_io)
    monkeypatch.setattr(lifecycle, "refresh", no_io)
    yield {**app_env, "owner": owner, "service": service, "order": order, "no_io": no_io}
    no_io.assert_not_awaited()
    shop_settings.invalidate()


def path(env):  # type: ignore[no-untyped-def]
    return f"/api/webapp/orders/{env['order'].id}/renewal-preview"


@pytest.mark.parametrize("mode", renewal_policy.RENEW_MODES)
@pytest.mark.parametrize("status", [ServiceStatus.ACTIVE, ServiceStatus.EXPIRED, ServiceStatus.LIMITED])
async def test_policy_before_after_matches_worker_without_writes(
    session, renewal_env, monkeypatch, mode, status
):  # type: ignore[no-untyped-def]
    env = renewal_env
    service, order, plan = (env[k] for k in ("service", "order", "plan"))
    plan.renew_mode = mode
    service.status = status
    await session.commit()
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr(renewal_policy, "_now", lambda: now)
    expected: dict[str, Any] = lifecycle._target_for(service, order, plan, mode)
    old_params = deepcopy(order.params)
    old_snapshot = renewal_policy.Snapshot.of(service).as_dict()
    before_counts = [await session.scalar(select(func.count(model.id))) for model in (Order, Payment, Job)]
    for _ in range(2):
        response = await env["client"].get(path(env), headers=h(89201))
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["basis"] == "stored_service_snapshot" and result["timing"] == "immediate"
        assert result["policy"] == expected["policy"] and result["resets_usage"] == expected["reset"]
        assert result["before"]["remaining_bytes"] == 7 * GB
        assert result["after"]["expire_at"] == expected["expire"]
        assert result["after"]["data_limit"] == expected["limit"]
        used = 0 if expected["reset"] else service.used_bytes
        assert result["after"]["used_bytes"] == used
        assert result["after"]["remaining_bytes"] == expected["limit"] - used
        assert "private_test_marker" not in response.text and "panel_username" not in response.text
        assert datetime.fromisoformat(result["valid_until"]) > datetime.fromisoformat(result["computed_at"])
    await session.refresh(order)
    await session.refresh(service)
    assert order.params == old_params and order.status == OrderStatus.PENDING
    assert renewal_policy.Snapshot.of(service).as_dict() == old_snapshot
    assert [
        await session.scalar(select(func.count(model.id))) for model in (Order, Payment, Job)
    ] == before_counts


async def test_unlimited_and_exhausted_have_distinct_remaining_values(session, renewal_env):  # type: ignore[no-untyped-def]
    env = renewal_env
    env["plan"].data_gb, env["plan"].days = 0, 0
    env["service"].data_limit = 0
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["before"]["remaining_bytes"] is None
    assert result["after"]["remaining_bytes"] is None and result["after"]["expire_at"] is None
    env["service"].data_limit = GB
    env["service"].used_bytes = 2 * GB
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["before"]["remaining_bytes"] == 0


async def test_queued_forecast_never_invents_after_values(session, renewal_env):  # type: ignore[no-untyped-def]
    env = renewal_env
    env["plan"].renew_mode = "extend_carry"
    await shop_settings.set_many(session, env["shop"].id, {"renew_queue_when_active": True})
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["timing"] == "queued" and result["after"] is None
    assert result["queued_until"] == env["service"].expire_at.isoformat()
    assert result["policy"]["volume"] == "reset"  # same queued override as worker
    assert "target" not in env["order"].params
    env["service"].used_bytes = env["service"].data_limit
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["timing"] == "immediate" and result["after"] is not None


async def test_auth_order_service_and_shop_scope(session, renewal_env):  # type: ignore[no-untyped-def]
    env = renewal_env
    assert (await env["client"].get(path(env))).status_code == 401
    assert (await env["client"].get(path(env), headers=h(89202))).status_code == 404
    foreign = await factories.shop(session)
    env["order"].shop_id = foreign.id
    await session.commit()
    assert (await env["client"].get(path(env), headers=h(89201))).status_code == 404
    env["order"].shop_id = env["shop"].id
    env["service"].shop_id = foreign.id
    await session.commit()
    assert (await env["client"].get(path(env), headers=h(89201))).status_code == 404
    env["service"].shop_id = env["shop"].id
    other = await factories.customer(session, env["shop"])
    env["service"].customer_id = other.id
    await session.commit()
    assert (await env["client"].get(path(env), headers=h(89201))).status_code == 404


@pytest.mark.parametrize(
    "condition",
    [
        "expired",
        "paid",
        "other_kind",
        "deleted",
        "trial",
        "disabled_panel",
        "nonrenewable",
        "wrong_panel",
        "busy",
    ],
)
async def test_unavailable_and_inflight_changes_fail_closed(session, renewal_env, condition):  # type: ignore[no-untyped-def]
    env = renewal_env
    order, service, plan = (env[k] for k in ("order", "service", "plan"))
    if condition == "expired":
        order.expires_at = utcnow() - timedelta(seconds=1)
    elif condition == "paid":
        order.status = OrderStatus.PAID
    elif condition == "other_kind":
        order.kind = OrderKind.PURCHASE
    elif condition == "deleted":
        service.status = ServiceStatus.DELETED
    elif condition == "trial":
        service.is_trial = True
    elif condition == "disabled_panel":
        env["panel"].options = {**(env["panel"].options or {}), "allow_renewals": False}
    elif condition == "nonrenewable":
        plan.renewable = False
    elif condition == "wrong_panel":
        plan.panel_id = (await factories.panel(session, env["shop"])).id
    else:
        session.add(
            Order(
                shop_id=order.shop_id,
                customer_id=order.customer_id,
                kind=OrderKind.RENEW,
                service_id=service.id,
                plan_id=plan.id,
                amount=90_000,
                status=OrderStatus.PROVISIONING,
            )
        )
    await session.commit()
    response = await env["client"].get(path(env), headers=h(89201))
    assert response.status_code == (404 if condition == "deleted" else 409), response.text
    assert "after" not in response.json()


async def test_mode_resolution_and_refresh_follow_worker_configuration(session, renewal_env):  # type: ignore[no-untyped-def]
    env = renewal_env
    plan, panel = env["plan"], env["panel"]
    plan.renew_mode = None
    panel.options = {"renew_resets_usage": False}
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["policy"]["volume"] == "append"
    panel.options = {}
    await shop_settings.set_many(session, env["shop"].id, {"renew_mode": "restart_reset"})
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["policy"]["time"] == "reset"
    plan.renew_mode = "extend_carry"
    await session.commit()
    result = (await env["client"].get(path(env), headers=h(89201))).json()
    assert result["policy"]["time"] == result["policy"]["volume"] == "append"
