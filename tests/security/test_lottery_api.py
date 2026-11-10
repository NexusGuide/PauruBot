"""Lottery admin API (GRW-04): ranked prizes, purchase points and the standings, kept to the
staff's own shop."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_points_lottery_with_ranked_prizes_and_standings(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    ends = (utcnow() + timedelta(days=7)).isoformat()
    body = {
        "title": "Azar",
        "mode": "points",
        "pick": "top",
        "points_per": 10_000,
        "board": "top",
        "prizes": [{"kind": "gb", "value": 10}, {"kind": "wallet", "value": 10_000, "label": "second"}],
        "ends_at": ends,
    }
    # points need a toman-per-point value; a prize needs a sane value
    assert (await owner.call("POST", "/api/admin/lotteries", {**body, "points_per": 0})).status_code == 422
    bad = {**body, "prizes": [{"kind": "gb", "value": 0}]}
    assert (await owner.call("POST", "/api/admin/lotteries", bad)).status_code == 422
    created = await owner.call("POST", "/api/admin/lotteries", body)
    assert created.status_code == 200, created.text
    lottery = created.json()
    assert lottery["winners"] == 2 and lottery["prizes"][1] == {
        "kind": "wallet",
        "value": 10_000,
        "label": "second",
    }

    session.add(
        Order(
            shop_id=env.shop.id,
            customer_id=env.customer_id,
            kind=OrderKind.PURCHASE,
            status=OrderStatus.COMPLETED,
            amount=45_000,
            completed_at=utcnow(),
        )
    )
    await session.commit()
    standings = await owner.call("GET", f"/api/admin/lotteries/{lottery['id']}/standings")
    assert standings.status_code == 200
    data = standings.json()
    assert data["total"] == 1 and data["items"][0]["customer_id"] == env.customer_id
    assert (
        data["items"][0]["points"] == 4
        and data["items"][0]["rank"] == 1
        and "telegram_id" in data["items"][0]
    )

    listed = (await owner.call("GET", "/api/admin/lotteries")).json()["items"][0]
    assert listed["entrants"] == 1 and listed["tickets"] == 4

    changed = await owner.call(
        "PUT", f"/api/admin/lotteries/{lottery['id']}/prizes", {"prizes": [{"kind": "days", "value": 5}]}
    )
    assert changed.status_code == 200 and changed.json()["winners"] == 1
    empty = await owner.call("PUT", f"/api/admin/lotteries/{lottery['id']}/prizes", {"prizes": []})
    assert empty.status_code == 422

    other = await login(http, "other_owner")
    assert (await other.call("GET", f"/api/admin/lotteries/{lottery['id']}/standings")).status_code == 404
    put = await other.call("PUT", f"/api/admin/lotteries/{lottery['id']}/prizes", {"prizes": []})
    assert put.status_code in {404, 422}

    owner = await login(http, "u_owner")  # one cookie jar: sign the owner back in
    drawn = await owner.call("POST", f"/api/admin/lotteries/{lottery['id']}/draw")
    assert drawn.status_code == 200 and drawn.json()["status"] == "drawn"
    result = drawn.json()["result"]
    assert result["winners"] == [env.customer_id] and result["awards"][0]["status"] == "no_service"
    late = await owner.call(
        "PUT", f"/api/admin/lotteries/{lottery['id']}/prizes", {"prizes": [{"kind": "days", "value": 5}]}
    )
    assert late.status_code == 409
