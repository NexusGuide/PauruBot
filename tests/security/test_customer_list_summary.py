"""The customer list says, per row, how many services are active, when the nearest one ends and
what is in the wallet; quick segments pick customers by their services and purchases. Additive:
the existing fields and filters are unchanged."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus, Service, ServiceStatus
from paurubot.ledger import wallet
from tests import factories
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_rows_carry_services_expiry_and_wallet(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    ending = utcnow() + timedelta(days=2)
    session.add(
        Service(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            panel_id=env.panel.id,
            panel_username="c_2",
            status=ServiceStatus.ACTIVE,
            expire_at=ending,
        )
    )
    session.add(
        Service(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            panel_id=env.panel.id,
            panel_username="c_old",
            status=ServiceStatus.EXPIRED,
        )
    )
    await wallet.adjust(session, env.shop.id, env.customer_id, 120_000, key="t-list-1", actor="test")
    await session.commit()
    owner = await login(http, "u_owner")
    rows = (await owner.call("GET", "/api/admin/customers?size=50")).json()["items"]
    mine = next(row for row in rows if row["id"] == env.customer_id)
    assert mine["active_services"] == 2  # the expired one is not counted
    assert mine["balance"] == 120_000
    assert mine["next_expiry"] is not None and mine["next_expiry"].startswith(ending.date().isoformat())
    other = next(row for row in rows if row["id"] == env.reseller_customer_id)
    assert other["active_services"] == 1 and other["balance"] == 0 and other["next_expiry"] is None
    assert "username" in mine and "tags" in mine  # the old fields stay


async def test_quick_segments(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    fresh = await factories.customer(session, env.shop, username="fresh")
    session.add(
        Service(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            panel_id=env.panel.id,
            panel_username="c_soon",
            status=ServiceStatus.ACTIVE,
            expire_at=utcnow() + timedelta(days=1),
        )
    )
    session.add(
        Order(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            kind=OrderKind.PURCHASE,
            status=OrderStatus.COMPLETED,
            amount=100_000,
        )
    )
    await session.commit()
    owner = await login(http, "u_owner")

    async def ids(segment: str) -> set[int]:
        resp = await owner.call("GET", f"/api/admin/customers?size=50&segment={segment}")
        assert resp.status_code == 200, resp.text
        return {row["id"] for row in resp.json()["items"]}

    assert await ids("active") == {env.customer_id, env.reseller_customer_id}
    assert await ids("ending") == {env.customer_id}
    assert await ids("buyers") == {env.customer_id}
    assert fresh.id in await ids("none") and env.customer_id not in await ids("none")
    assert (await owner.call("GET", "/api/admin/customers?segment=nope")).status_code == 422
