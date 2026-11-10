"""Categories and locations can be deleted, not only hidden (CAT-01): a category's plans stay on
sale without one; a location goes only when nothing is sold through it alone."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.core import settings as shop_settings
from paurubot.db.models import AccountPlan, AuditLog, Category, Location, Panel, PanelKind, Plan
from tests import factories
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_deleting_a_category_keeps_its_plans_on_sale(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    category = Category(shop_id=env.shop.id, title="TUNNEL")
    session.add(category)
    await session.flush()
    plan = await factories.plan(session, env.shop, env.panel)
    plan.category_id = category.id
    pasarguard = await factories.panel(session, env.shop, PanelKind.PASARGUARD)
    account_plan = await factories.account_plan(session, env.shop, pasarguard)
    account_plan.category_id = category.id
    await session.commit()
    category_id, plan_id, account_plan_id = category.id, plan.id, account_plan.id

    support = await login(http, "u_support")
    assert (await support.call("DELETE", f"/api/admin/categories/{category_id}")).status_code == 403
    other = await login(http, "other_owner")
    assert (await other.call("DELETE", f"/api/admin/categories/{category_id}")).status_code == 404

    owner = await login(http, "u_owner")
    done = await owner.call("DELETE", f"/api/admin/categories/{category_id}")
    assert done.status_code == 200 and done.json() == {"deleted": True, "plans_moved": 2}
    session.expire_all()
    assert await session.get(Category, category_id) is None
    kept = await session.get(Plan, plan_id)
    kept_account = await session.get(AccountPlan, account_plan_id)
    assert kept is not None and kept.category_id is None and kept.is_active
    assert kept_account is not None and kept_account.category_id is None
    assert (await owner.call("DELETE", f"/api/admin/categories/{category_id}")).status_code == 404
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "category.delete")) is not None


async def test_a_location_goes_only_when_nothing_needs_it(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    location = Location(shop_id=env.shop.id, title="Germany")
    session.add(location)
    await session.flush()
    panel = await session.get(Panel, env.panel.id)
    assert panel is not None
    panel.location_id = location.id
    bound = await factories.plan(session, env.shop, env.panel)  # sold on its own panel
    bound.location_id = location.id
    pooled = Plan(shop_id=env.shop.id, title="By location", price=1, location_id=location.id)  # no panel
    session.add(pooled)
    await session.commit()
    location_id, bound_id, panel_id = location.id, bound.id, env.panel.id

    owner = await login(http, "u_owner")
    refused = await owner.call("DELETE", f"/api/admin/locations/{location_id}")
    assert refused.status_code == 409 and refused.json()["error"] == "location_in_use"

    pooled.panel_id = panel_id  # give it a panel: nothing depends on the location now
    await shop_settings.set_many(session, env.shop.id, {"custom_location_id": location_id})
    await session.commit()
    custom = await owner.call("DELETE", f"/api/admin/locations/{location_id}")
    assert custom.status_code == 409 and custom.json()["error"] == "location_in_use_custom"

    await shop_settings.set_many(session, env.shop.id, {"custom_location_id": None})
    await session.commit()
    done = await owner.call("DELETE", f"/api/admin/locations/{location_id}")
    assert done.status_code == 200, done.text
    session.expire_all()
    assert await session.get(Location, location_id) is None
    panel = await session.get(Panel, panel_id)
    assert panel is not None and panel.location_id is None  # only the label is gone
    assert (await session.get(Plan, bound_id)).location_id is None
