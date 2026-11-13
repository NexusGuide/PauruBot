"""Staff keep a lapsed service out of the expired-service cleanup, for some days or for good."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from paurubot.db.models import AuditLog, Service
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_staff_keep_a_service_for_a_while_or_for_good(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    service = await session.get(Service, env.other_service_id)
    assert service is not None
    service.cleanup_warned_at = datetime.now(UTC)
    await session.commit()
    owner = await login(http, "u_owner")
    path = f"/api/admin/services/{env.other_service_id}/keep"

    kept = await owner.call("PUT", path, {"days": 10})
    assert kept.status_code == 200, kept.text
    out = kept.json()["service"]
    until = datetime.fromisoformat(out["keep_until"])
    assert timedelta(days=9.9) < until - datetime.now(UTC) <= timedelta(days=10)
    assert out["cleanup_exempt"] is False
    await session.refresh(service)
    assert service.cleanup_warned_at is None, "a new date is warned about again"

    out = (await owner.call("PUT", path, {"forever": True})).json()["service"]
    assert out["cleanup_exempt"] is True and out["keep_until"] is None

    out = (await owner.call("PUT", path, {})).json()["service"]
    assert out["cleanup_exempt"] is False and out["keep_until"] is None, "back to the shop's rule"

    actions = list(
        await session.scalars(select(AuditLog.action).where(AuditLog.entity_id == env.other_service_id))
    )
    assert actions.count("service.keep") == 3

    assert (await owner.call("PUT", path, {"days": 4000})).status_code == 422
    assert (await owner.call("PUT", path, {"days": 5, "forever": True})).status_code == 422


async def test_a_reseller_keeps_only_its_own_customers_services(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    reseller = await login(http, "u_reseller")
    assert (
        await reseller.call("PUT", f"/api/admin/services/{env.other_service_id}/keep", {"days": 3})
    ).status_code == 404
    mine = await reseller.call("PUT", f"/api/admin/services/{env.reseller_service_id}/keep", {"days": 3})
    assert mine.status_code == 200, mine.text


async def test_keeping_needs_the_service_write_permission(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    support = await login(http, "u_support")
    resp = await support.call("PUT", f"/api/admin/services/{env.other_service_id}/keep", {"days": 3})
    assert resp.status_code == 200, "support may change services (services.write)"
    finance = await login(http, "u_finance")
    assert (
        await finance.call("PUT", f"/api/admin/services/{env.other_service_id}/keep", {"days": 3})
    ).status_code == 403
