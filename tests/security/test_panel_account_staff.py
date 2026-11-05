"""Staff control of sold panel admin accounts: give one, gift time and traffic, reset usage, a new
password, the user cap, the plan, a note and a bulk gift — each audited and told to the owner."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from paurubot.core import panel_accounts
from paurubot.core import settings as shop_settings
from paurubot.db.models import AccountPlan, AuditLog, Job, PanelAccount, PanelKind
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)
from tests.unit.test_resellers import drain

pytestmark = pytest.mark.security


@pytest.fixture
async def sold(session, env):  # type: ignore[no-untyped-def]  # noqa: F811
    panel = await factories.panel(session, env.shop, PanelKind.PASARGUARD)
    fake = FakeTokenPanel("pasarguard")
    registry.test_transports[panel.id] = transport(fake)
    plan = AccountPlan(
        shop_id=env.shop.id, panel_id=panel.id, title="Pro", role_id=2, data_gb=100, days=30, price=1
    )
    other = AccountPlan(
        shop_id=env.shop.id, panel_id=panel.id, title="Max", role_id=3, data_gb=900, days=30, price=2
    )
    session.add_all([plan, other])
    await shop_settings.set_many(session, env.shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    yield {"panel": panel, "fake": fake, "plan": plan, "other": other}
    registry.test_transports.clear()
    registry._cache.clear()


async def test_staff_give_change_and_gift_accounts(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    fake = sold["fake"]
    given = await owner.call(
        "POST", "/api/admin/panel-accounts", {"customer_id": env.customer_id, "plan_id": sold["plan"].id}
    )
    assert given.status_code == 200, given.text
    await drain(session, "provision")
    account = await session.scalar(select(PanelAccount).where(PanelAccount.customer_id == env.customer_id))
    assert account is not None and account.status == "active" and account.data_limit == 100 * GB

    async def change(body: dict[str, Any]) -> Any:
        return await owner.call("POST", f"/api/admin/panel-accounts/{account.id}/change", body)

    resp = await change({"action": "gift", "days": 7, "gb": 50, "reason": "outage"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["data_limit"] == 150 * GB
    assert (await change({"action": "gift"})).status_code == 422
    fake.sold_admins[account.username]["used_traffic"] = 40 * GB
    assert (await change({"action": "reset_usage", "notify": False})).status_code == 200
    assert fake.sold_admins[account.username]["used_traffic"] == 0
    old_password = panel_accounts.password_of(account)
    assert (await change({"action": "password", "notify": False})).status_code == 200
    await session.refresh(account)
    assert panel_accounts.password_of(account) != old_password
    assert (await change({"action": "users", "max_users": 25})).json()["item"]["max_users"] == 25
    assert (await change({"action": "plan", "plan_id": sold["other"].id})).json()["item"]["plan_id"] == sold[
        "other"
    ].id
    note = await owner.call("PUT", f"/api/admin/panel-accounts/{account.id}/note", {"note": " VIP "})
    assert note.json()["item"]["note"] == "VIP"

    # Each change is audited, and the owner hears of it (the password always, with the login).
    actions = list(await session.scalars(select(AuditLog.action).where(AuditLog.entity == "panel_account")))
    for action in ("gift", "reset_usage", "password", "users", "plan", "note"):
        assert f"panel_account.{action}" in actions, action
    told = [
        (j.payload["change"], j.payload["login"])
        for j in await session.scalars(select(Job).where(Job.kind == "account_changed"))
    ]
    assert ("gift", False) in told and ("password", True) in told
    assert all(change != "reset_usage" for change, _ in told)  # notify was off

    bulk = await owner.call(
        "POST", "/api/admin/panel-accounts/bulk/gift", {"ids": [account.id, 999_999], "days": 1, "gb": 0}
    )
    assert bulk.status_code == 200
    assert bulk.json()["done"] == [account.id]
    assert bulk.json()["failed"] == [{"id": 999_999, "error": "not_found"}]


async def test_only_account_managers_may_change_accounts(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    support = await login(http, "u_support")
    resp = await support.call(
        "POST", "/api/admin/panel-accounts", {"customer_id": env.customer_id, "plan_id": sold["plan"].id}
    )
    assert resp.status_code == 403
    resp = await support.call("POST", "/api/admin/panel-accounts/bulk/gift", {"ids": [1], "days": 1})
    assert resp.status_code == 403


async def test_a_deleted_account_takes_no_staff_changes(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    customer = await session.get(
        __import__("paurubot.db.models", fromlist=["Customer"]).Customer, env.customer_id
    )
    account = PanelAccount(
        shop_id=env.shop.id,
        customer_id=customer.id,
        panel_id=sold["panel"].id,
        plan_id=sold["plan"].id,
        username="r_gone",
        password_enc="x",
        status="deleted",
    )
    session.add(account)
    await session.commit()
    for body in (
        {"action": "password"},
        {"action": "users", "max_users": 5},
        {"action": "plan", "plan_id": sold["other"].id},
    ):
        resp = await owner.call("POST", f"/api/admin/panel-accounts/{account.id}/change", body)
        assert resp.status_code == 409 and resp.json()["error"] == "account_not_found", body
