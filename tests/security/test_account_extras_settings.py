"""The least and most extra GB and days an admin account owner may buy are checked when saved."""

from __future__ import annotations

from typing import Any

import pytest

from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_admin_account_extra_ranges_are_checked_when_saved(http, env) -> None:  # noqa: F811
    owner = await login(http, "u_owner")

    async def put(values: dict[str, Any]) -> Any:
        return await owner.call("PUT", "/api/admin/settings", {"values": values})

    assert (await put({"panel_accounts_days_min": 5, "panel_accounts_days_max": 90})).status_code == 200
    for bad in (
        {"panel_accounts_days_min": 100},  # above the saved most (90)
        {"panel_accounts_gb_min": 0},
        {"panel_accounts_gb_max": 200_000},
        {"panel_accounts_days_max": "x"},
    ):
        resp = await put(bad)
        assert resp.status_code == 422 and resp.json()["error"] == "account_extras_invalid", bad
    assert (await put({"panel_accounts_gb_min": 50, "panel_accounts_gb_max": 50})).status_code == 200


async def test_an_account_plan_saves_its_renewal_options_and_ranges(session, http, env) -> None:  # noqa: F811
    from paurubot.db.models import PanelKind
    from tests import factories

    pasarguard = await factories.panel(session, env.shop, PanelKind.PASARGUARD)
    await session.commit()
    owner = await login(http, "u_owner")
    body = {
        "panel_id": pasarguard.id,
        "title": "Admin 500",
        "role_id": 2,
        "data_gb": 500,
        "price": 900_000,
        "day_price": 5_000,
        "days_min": 7,
        "days_max": 60,
        "renew_packages_enabled": True,
        "renew_packages": [{"days": 30, "gb": 500, "price": 850_000}],
        "renew_custom_enabled": True,
    }
    created = await owner.call("POST", "/api/admin/account-plans", body)
    assert created.status_code == 200, created.text
    saved = created.json()
    assert saved["renew_packages"] == [{"days": 30, "gb": 500, "price": 850_000}]
    assert (saved["days_min"], saved["days_max"], saved["gb_min"]) == (7, 60, None)
    assert saved["renew_packages_enabled"] is True and saved["renew_custom_enabled"] is True
    for bad in (
        {"renew_packages": [{"days": 0, "gb": 1, "price": 1}]},
        {"renew_packages": [{"days": 30, "gb": 1}]},
        {"days_min": 61, "days_max": 60},
        {"gb_min": 0},
    ):
        resp = await owner.call("PUT", f"/api/admin/account-plans/{saved['id']}", {**body, **bad})
        assert resp.status_code == 422, bad
