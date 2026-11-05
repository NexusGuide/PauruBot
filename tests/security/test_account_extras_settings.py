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
