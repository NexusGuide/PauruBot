"""Quick links into the bot and the message variables reference (settings pages)."""

from __future__ import annotations

import pytest

from paurubot.core import settings as shop_settings
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_quick_links_follow_what_is_on(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    env.shop.bot_username = "pauru_bot"
    await shop_settings.set_many(session, env.shop.id, {"trial_enabled": False})
    await session.commit()
    owner = await login(http, "u_owner")
    data = (await owner.call("GET", "/api/admin/quick-links")).json()
    assert data["bot_username"] == "pauru_bot"
    items = {i["item"]: i for i in data["items"]}
    assert items["buy"] == {"item": "buy", "start": "go_buy", "on": True}
    assert items["trial"]["on"] is False
    assert "language" not in items and "miniapp" not in items
    admin = await login(http, "u_admin")  # settings.read is enough
    assert (await admin.call("GET", "/api/admin/quick-links")).status_code == 200
    support = await login(http, "u_support")
    assert (await support.call("GET", "/api/admin/quick-links")).status_code == 403


async def test_message_variables_list_where_each_is_used(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    items = (await owner.call("GET", "/api/admin/message-variables")).json()["items"]
    by_name = {i["name"]: i for i in items}
    assert by_name["amount"]["count"] >= 10 and len(by_name["amount"]["keys"]) <= 8
    assert items[0]["count"] >= items[-1]["count"]  # most used first
    finance = await login(http, "u_finance")
    assert (await finance.call("GET", "/api/admin/message-variables")).status_code == 403
