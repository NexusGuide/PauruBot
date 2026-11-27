"""Unit tests for trial & renewal bug fixes and multi-panel / sub-GB / hourly trials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from paurubot.core import auto_renew, catalog, orders, resale, trials
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Job,
    JobStatus,
    Location,
    Order,
    OrderKind,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.panels import GB
from paurubot.provisioning import lifecycle
from paurubot.provisioning import renewal_policy as rp
from tests import factories
from tests.unit.test_reseller_bot import TOKEN
from tests.unit.test_reseller_bot import env as env  # noqa: F401  (fixture)


def test_configured_trial_panel_ids_precedence() -> None:
    assert trials._configured_panel_ids({"trial_panel_ids": [5, 2], "trial_panel_id": 9}) == [5, 2]
    assert trials._configured_panel_ids({"trial_panel_ids": [], "trial_panel_id": 9}) == [9]
    assert trials._configured_panel_ids({"trial_panel_ids": [], "trial_panel_id": None}) == []


def test_renewal_policy_carry_queued_preserves_extend_carry() -> None:
    assert (
        rp.policy_for(OrderKind.RENEW, "extend_carry", queued=True, carry_queued=True)
        == rp.MODE_POLICIES["extend_carry"]
    )
    assert (
        rp.policy_for(OrderKind.RENEW, "extend_carry", queued=True, carry_queued=False)
        == rp.MODE_POLICIES["extend_reset"]
    )


def test_settle_service_preserves_on_hold_when_expire_unchanged() -> None:
    service = Service(
        status=ServiceStatus.ON_HOLD,
        data_limit=10 * GB,
        used_bytes=0,
        expire_at=None,
    )
    order = Order(kind=OrderKind.EXTRA_VOLUME)
    target = {
        "set_expire": False,
        "expire": None,
        "limit": 20 * GB,
        "reset": False,
        "before": {"status": ServiceStatus.ON_HOLD.value},
    }
    lifecycle._settle_service(service, order, None, target, verified=False)
    assert service.status == ServiceStatus.ON_HOLD
    assert service.data_limit == 20 * GB


async def test_trial_skips_unhealthy_national_and_full_capacity_panels(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    unhealthy = await factories.panel(session, shop)
    unhealthy.is_healthy = False
    national = await factories.panel(session, shop)
    national.national_mode = True
    full = await factories.panel(session, shop)
    full.capacity = 1
    good = await factories.panel(session, shop)
    customer = await factories.customer(session, shop)
    existing = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=full.id,
        panel_username="taken",
        status=ServiceStatus.ACTIVE,
        sub_token="tok-full-cap-1",
    )
    session.add(existing)
    await session.flush()

    avail = await trials.options(session, customer)
    assert [p.id for p in avail] == [good.id]
    order = await trials.request(session, customer)
    assert order.params["panel_id"] == good.id


async def test_multi_panel_trial_choice_and_sub_gb_hourly_params(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    p1 = await factories.panel(session, shop)
    p2 = await factories.panel(session, shop)
    p3 = await factories.panel(session, shop)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "trial_panel_ids": [p3.id, p1.id],
            "trial_choose_panel": True,
            "trial_hours": 6,
            "trial_mb": 300,
        },
    )
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()

    customer = await factories.customer(session, shop)
    opts = await trials.options(session, customer)
    assert [p.id for p in opts] == [p3.id, p1.id]

    with pytest.raises(trials.TrialError, match="trial_panel_unavailable"):
        await trials.request(session, customer, panel_id=p2.id)

    order = await trials.request(session, customer, panel_id=p1.id)
    assert order.params["panel_id"] == p1.id
    assert order.params["hours"] == 6
    assert order.params["mb"] == 300


async def test_trial_allow_again_uses_parent_generation_on_reseller_bot(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    await shop_settings.set_many(session, shop.id, {"resale_trial_fee": 0, "trial_generation": 4})
    child = await resale.connect(session, env["profile"], TOKEN, "Reseller Trial Bot")
    await resale.allow_feature(session, env["profile"], "trial", True)
    await session.commit()
    await resale.update(session, child, {"trial_enabled": True})
    await session.commit()
    shop_settings.invalidate(child.id)
    shop_settings.invalidate(shop.id)

    customer = await factories.customer(session, child, telegram_id=77001)
    await trials.request(session, customer)
    await session.commit()

    assert await trials.allow_again(session, customer) is True
    await session.commit()
    order2 = await trials.request(session, customer)
    assert order2.kind == OrderKind.TRIAL


async def test_location_plans_visible_for_service_and_cross_location_blocked(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    loc_de = Location(shop_id=shop.id, title="Germany", flag="DE", is_active=True)
    loc_tr = Location(shop_id=shop.id, title="Turkey", flag="TR", is_active=True)
    session.add_all([loc_de, loc_tr])
    await session.flush()

    panel_de = await factories.panel(session, shop)
    panel_de.location_id = loc_de.id
    panel_tr = await factories.panel(session, shop)
    panel_tr.location_id = loc_tr.id
    await session.flush()

    plan_de = Plan(
        shop_id=shop.id,
        title="DE 30G",
        panel_id=None,
        location_id=loc_de.id,
        days=30,
        data_gb=30,
        price=200_000,
        renewable=True,
        is_active=True,
    )
    plan_tr = Plan(
        shop_id=shop.id,
        title="TR 30G",
        panel_id=None,
        location_id=loc_tr.id,
        days=30,
        data_gb=30,
        price=90_000,
        renewable=True,
        is_active=True,
    )
    session.add_all([plan_de, plan_tr])
    customer = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel_de.id,
        plan_id=plan_de.id,
        panel_username="de_user",
        status=ServiceStatus.ACTIVE,
        expire_at=datetime.now(UTC) + timedelta(days=5),
        data_limit=30 * GB,
        sub_token="tok-loc-renew-1",
    )
    session.add(service)
    await session.flush()

    offered = await catalog.plans_for_service(session, shop.id, customer, service)
    assert [p.id for p in offered] == [plan_de.id]

    ok = await orders.draft(session, customer, OrderKind.RENEW, plan=plan_de, service=service)
    assert ok.plan == plan_de

    with pytest.raises(orders.OrderError, match="plan_wrong_panel"):
        await orders.draft(session, customer, OrderKind.RENEW, plan=plan_tr, service=service)


async def test_auto_renew_defers_instead_of_stopping_during_national_mode(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000)
    customer = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="auto_nat",
        status=ServiceStatus.ACTIVE,
        expire_at=datetime.now(UTC) + timedelta(minutes=30),
        data_limit=10 * GB,
        used_bytes=9 * GB,
        auto_renew={"mode": "renew", "plan_id": plan.id},
        sub_token="tok-auto-nat-1",
    )
    session.add(service)
    panel.national_mode = True
    await session.flush()

    outcome = await auto_renew.run_service(session, service.id)
    assert outcome == "national_mode"
    assert not (service.auto_renew or {}).get("stopped")
