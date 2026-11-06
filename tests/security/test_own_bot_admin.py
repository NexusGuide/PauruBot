"""The dedicated own-panel reseller bot from a customer's page (OPB-3): only the shop's owner and
admins connect, pause and stop it; the token is entered by staff; everything is audited."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.core import own_panel, resale
from paurubot.core import settings as shop_settings
from paurubot.db.models import AuditLog, Customer, PanelKind, Shop
from paurubot.panels import registry
from paurubot.security.secretbox import encrypt
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security
TOKEN = "887766554:AAH-own-panel-bot-token-for-tests-only-x"


class _EveryPanel(dict):  # type: ignore[type-arg]
    def __init__(self, shared) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.shared = shared

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        return self.shared


@pytest.fixture
async def sold(session, env, monkeypatch):  # type: ignore[no-untyped-def]  # noqa: F811
    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "ali_own_bot"

    async def no_webhook(shop_id: int) -> None:
        return None

    monkeypatch.setattr(resale, "bot_identity", identity)
    monkeypatch.setattr(resale, "point_webhook", no_webhook)
    monkeypatch.setattr(registry, "test_transports", _EveryPanel(transport(FakeTokenPanel("pasarguard"))))
    panel = await factories.panel(session, env.shop, PanelKind.PASARGUARD)
    plan = await factories.account_plan(session, env.shop, panel)
    customer = await session.get(Customer, env.customer_id)
    assert customer is not None
    account = await factories.panel_account(session, customer, plan)
    account.password_enc = encrypt("panel", "panel-password")
    await session.commit()
    yield {"account": account}
    registry._cache.clear()


async def test_owner_connects_pauses_and_stops_a_customers_bot(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    base = f"/api/admin/customers/{env.customer_id}/own-bot"
    view = (await owner.call("GET", base)).json()
    assert view["bot"] is None and [a["id"] for a in view["accounts"]] == [sold["account"].id]
    assert view["accounts"][0]["usable"] is True

    assert (
        await owner.call("POST", base, {"account_id": sold["account"].id})
    ).status_code == 422  # no token yet
    made = await owner.call("POST", base, {"account_id": sold["account"].id, "token": TOKEN, "title": "Ali"})
    assert made.status_code == 200, made.text
    bot = made.json()["bot"]
    assert bot["connected"] is True and bot["bot_username"] == "ali_own_bot" and bot["paused"] == ""
    child = await session.get(Shop, bot["shop_id"])
    assert child is not None and child.own_account_id == sold["account"].id

    held = await owner.call("POST", f"{base}/hold", {"hold": True})
    assert held.json()["bot"]["hold"] is True
    assert await own_panel.sales_gate(session, child.id, new_user=False) == "own_sales_paused"
    assert (await owner.call("POST", f"{base}/hold", {"hold": False})).json()["bot"]["hold"] is False

    stopped = await owner.call("DELETE", base)
    assert stopped.json()["bot"]["connected"] is False
    actions = set(await session.scalars(select(AuditLog.action).where(AuditLog.action.like("own_bot.%"))))
    assert {"own_bot.connect", "own_bot.hold", "own_bot.release", "own_bot.disconnect"} <= actions


async def test_only_the_shops_managers_reach_it(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    base = f"/api/admin/customers/{env.customer_id}/own-bot"
    for username in ("u_reseller", "u_support", "u_finance", "u_merchant"):
        staff = await login(http, username)
        assert (await staff.call("GET", base)).status_code in (403, 404), username
        assert (
            await staff.call("POST", base, {"account_id": sold["account"].id, "token": TOKEN})
        ).status_code in (403, 404)
    other = await login(http, "other_owner")  # another shop never sees this customer
    assert (await other.call("GET", base)).status_code == 404


async def test_a_bad_token_or_someone_elses_account_is_refused(session, http, env, sold) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    base = f"/api/admin/customers/{env.customer_id}/own-bot"
    bad = await owner.call("POST", base, {"account_id": sold["account"].id, "token": "nope"})
    assert bad.status_code == 422 and bad.json()["error"] == "bot_token_invalid"
    stranger = await factories.customer(session, env.shop, telegram_id=73001)
    theirs = await owner.call(
        "POST",
        f"/api/admin/customers/{stranger.id}/own-bot",
        {"account_id": sold["account"].id, "token": TOKEN},
    )
    assert theirs.status_code == 404  # an account is only ever linked to its own buyer
    assert (await shop_settings.load(session, env.shop.id, fresh=True)).get(own_panel.PAUSED) == ""
