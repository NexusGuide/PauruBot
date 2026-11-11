"""Staff review customers' requests to add a previous service, from the web panel."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.db.models import AuditLog, Job, ServiceClaim
from paurubot.panels import registry
from tests.fakes.panels import FakeTokenPanel, transport
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)
from tests.unit.test_importer import _panel_user

pytestmark = pytest.mark.security


async def test_staff_approve_or_reject_claims(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    fake = FakeTokenPanel("marzban")
    registry.test_transports[env.panel.id] = transport(fake)
    try:
        _panel_user(fake, "claimed_a")
        _panel_user(fake, "claimed_b")
        a = ServiceClaim(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            panel_id=env.panel.id,
            panel_username="claimed_a",
        )
        b = ServiceClaim(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            panel_id=env.panel.id,
            panel_username="claimed_b",
        )
        session.add_all([a, b])
        await session.commit()
        owner = await login(http, "u_owner")
        items = (await owner.call("GET", "/api/admin/service-claims")).json()["items"]
        assert [i["panel_username"] for i in items] == ["claimed_b", "claimed_a"]
        assert items[0]["customer"]["id"] == env.customer_id and items[0]["panel"]["id"] == env.panel.id
        ok = await owner.call("POST", f"/api/admin/service-claims/{a.id}", {"approve": True})
        assert (
            ok.status_code == 200
            and ok.json()["claim"]["status"] == "approved"
            and ok.json()["claim"]["service_id"]
        )
        no = await owner.call("POST", f"/api/admin/service-claims/{b.id}", {"approve": False})
        assert no.json()["claim"]["status"] == "rejected"
        again = await owner.call("POST", f"/api/admin/service-claims/{b.id}", {"approve": True})
        assert again.status_code == 422 and again.json()["error"] == "import_decided"
        notes = list(await session.scalars(select(Job.kind).where(Job.kind == "service_claim_decided")))
        assert len(notes) == 2
        assert set(
            await session.scalars(select(AuditLog.action).where(AuditLog.action.like("service_claim.%")))
        ) == {
            "service_claim.approved",
            "service_claim.rejected",
        }
        assert (await owner.call("GET", "/api/admin/service-claims?status=pending")).json()["items"] == []
        for username in ("u_reseller", "u_finance"):
            staff = await login(http, username)
            assert (await staff.call("GET", "/api/admin/service-claims")).status_code == 403, username
        other = await login(http, "other_owner")
        assert (
            await other.call("POST", f"/api/admin/service-claims/{a.id}", {"approve": False})
        ).status_code == 404
    finally:
        registry.test_transports.clear()
        registry._cache.clear()
