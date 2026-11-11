"""The panel's button looks page: every button, its label and its color / premium emoji."""

from __future__ import annotations

import pytest

from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_every_button_is_listed_and_its_look_saved(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    page = (await owner.call("GET", "/api/admin/button-looks")).json()
    keys = {item["key"] for item in page["items"]}
    assert {"btn_buy", "btn_back", "adm_nav_home"} <= keys and len(keys) > 150
    assert page["looks"] == {}
    saved = await owner.call(
        "PUT", "/api/admin/settings", {"values": {"button_looks": {"btn_buy": {"style": "success"}}}}
    )
    assert saved.status_code == 200, saved.text
    assert (await owner.call("GET", "/api/admin/button-looks")).json()["looks"] == {
        "btn_buy": {"style": "success"}
    }
    bad = await owner.call(
        "PUT", "/api/admin/settings", {"values": {"button_looks": {"btn_buy": {"style": "pink"}}}}
    )
    assert bad.status_code == 422
