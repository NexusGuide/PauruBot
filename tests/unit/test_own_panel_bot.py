"""A dedicated reseller bot on the reseller's own panel (OPB)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.core import own_panel, panel_accounts, resale, usernames
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Job,
    JobStatus,
    Panel,
    PanelAccount,
    PanelKind,
    ResellerProfile,
    ShopStatus,
    Staff,
    StaffRole,
    UsernameMethod,
)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.security.secretbox import encrypt
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot

TOKEN = "887766554:AAH-own-panel-bot-token-for-tests-only-x"


class _EveryPanel(dict):  # type: ignore[type-arg]
    """One fake PasarGuard server behind every panel row, including the child shop's own one."""

    def __init__(self, shared) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.shared = shared

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        return self.shared


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))

    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "ali_own_bot"

    monkeypatch.setattr(resale, "bot_identity", identity)
    fake = FakeTokenPanel("pasarguard")
    monkeypatch.setattr(registry, "test_transports", _EveryPanel(transport(fake)))
    shop = await factories.shop(session, "Main")
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    account_plan = await factories.account_plan(session, shop, panel)
    owner = await factories.customer(session, shop, telegram_id=71001)
    account = await factories.panel_account(session, owner, account_plan)
    account.password_enc = encrypt("panel", "panel-password")  # the fake server's login
    fake.sold_admins[account.username] = {"username": account.username, "status": "active"}
    await session.commit()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    yield {
        "shop": shop,
        "panel": panel,
        "owner": owner,
        "account": account,
        "fake": fake,
        "plan": account_plan,
        "tg": tg,
    }
    notify_telegram.set_bot(None)
    registry._cache.clear()


async def test_connect_makes_an_isolated_shop_on_the_purchased_account(session, env) -> None:  # type: ignore[no-untyped-def]
    """An ordinary customer's bot sells from its own admin account: its own shop, catalog and
    panel, no reseller profile, so the shop-panels mode never touches it."""
    child = await own_panel.connect(session, env["account"], TOKEN, "Ali shop")
    await session.commit()
    assert child.parent_id == env["shop"].id and child.reseller_profile_id is None
    assert child.own_account_id == env["account"].id and child.bot_username == "ali_own_bot"
    assert child.status == ShopStatus.ACTIVE and child.title == "Ali shop"
    assert await resale.for_shop(session, child.id) is None  # not a shop-panels reseller bot
    assert await session.scalar(select(resale.catalog_owner(child.id))) == child.id  # its own catalog
    assert (
        await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == env["owner"].id))
        is None
    )

    panel = await session.scalar(select(Panel).where(Panel.shop_id == child.id))
    assert panel is not None and panel.kind == PanelKind.PASARGUARD
    assert panel.base_url == env["panel"].base_url
    assert panel.username == env["account"].username and panel.password_enc == env["account"].password_enc
    assert panel.username_method == UsernameMethod.CUSTOMER_NAME

    cfg = await shop_settings.load(session, child.id, fresh=True)
    assert (
        cfg["card_enabled"] is True
        and cfg["trial_enabled"] is False
        and cfg["panel_accounts_enabled"] is False
    )
    merchant = await session.scalar(select(Staff).where(Staff.shop_id == child.id))
    assert merchant is not None and merchant.role == StaffRole.MERCHANT
    assert merchant.telegram_id == env["owner"].telegram_id
    assert child.id in [s.id for s, _ in await resale.bots(session)]  # served like reseller bots


async def test_connect_refuses_what_cannot_serve(session, env) -> None:  # type: ignore[no-untyped-def]
    account = env["account"]
    account.status = "expired"
    with pytest.raises(own_panel.OwnPanelError) as exc:
        await own_panel.connect(session, account, TOKEN)
    assert exc.value.key == "own_panel_account_unusable"
    account.status = "active"
    with pytest.raises(own_panel.OwnPanelError) as exc:
        await own_panel.connect(session, account, "not-a-token")
    assert exc.value.key == "bot_token_invalid"
    env["fake"].groups_permission = None  # this admin may read no groups at all
    other = await factories.customer(session, env["shop"], telegram_id=71002)
    other_account = await factories.panel_account(session, other, env["plan"])
    await own_panel.connect(session, other_account, TOKEN, verify=False)
    with pytest.raises(own_panel.OwnPanelError) as exc:
        await own_panel.connect(session, account, TOKEN, verify=False)  # the same bot elsewhere
    assert exc.value.key == "bot_token_in_use"


async def test_unreachable_panel_is_refused_before_the_bot_goes_live(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def down(panel):  # type: ignore[no-untyped-def]
        raise own_panel.OwnPanelError("own_panel_unreachable")

    monkeypatch.setattr(own_panel, "check_login", down)
    with pytest.raises(own_panel.OwnPanelError) as exc:
        await own_panel.connect(session, env["account"], TOKEN)
    assert exc.value.key == "own_panel_unreachable"


async def test_reconnecting_keeps_the_shop_and_follows_the_new_account(session, env) -> None:  # type: ignore[no-untyped-def]
    first = await own_panel.connect(session, env["account"], TOKEN, verify=False)
    second_account = PanelAccount(
        shop_id=env["owner"].shop_id,
        customer_id=env["owner"].id,
        panel_id=env["plan"].panel_id,
        plan_id=env["plan"].id,
        username="r71001_second",
        password_enc=encrypt("panel", "panel-password"),
    )
    session.add(second_account)
    await session.flush()
    again = await own_panel.connect(session, second_account, TOKEN, verify=False)
    assert again.id == first.id and again.own_account_id == second_account.id
    panel = await session.scalar(select(Panel).where(Panel.shop_id == again.id))
    assert panel is not None and panel.username == "r71001_second"
    assert len(list(await session.scalars(select(Panel).where(Panel.shop_id == again.id)))) == 1


async def test_password_change_moves_the_bots_login_too(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await own_panel.connect(session, env["account"], TOKEN, verify=False)
    await panel_accounts.change_password(session, env["account"])
    panel = await session.scalar(select(Panel).where(Panel.shop_id == child.id))
    assert panel is not None and panel.password_enc == env["account"].password_enc


async def test_disconnect_stops_the_bot_and_keeps_its_records(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await own_panel.connect(session, env["account"], TOKEN, verify=False)
    await own_panel.disconnect(session, child)
    assert child.status == ShopStatus.SUSPENDED and child.bot_token_enc is None
    assert child.own_account_id == env["account"].id  # its customers, plans and payments stay
    assert child.id not in [s.id for s, _ in await resale.bots(session)]


async def test_own_bot_messages_go_through_its_own_bot(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    child = await own_panel.connect(session, env["account"], TOKEN, verify=False)
    await session.commit()
    monkeypatch.setattr(notify_telegram, "_single", False)  # a real install: one bot per shop
    notify_telegram.forget_shop_bot(child.id)
    bot = await notify_telegram.bot_for(child.id)
    assert bot is not None and bot is not notify_telegram.get_bot()  # never the main bot


async def test_config_names_are_the_customers_name_with_a_random_tail(session, env) -> None:  # type: ignore[no-untyped-def]
    panel = env["panel"]
    panel.username_method = UsernameMethod.CUSTOMER_NAME
    named = await factories.customer(session, env["shop"], telegram_id=71003, username="Ali_VPN")
    first = await usernames.generate(session, panel, named)
    second = await usernames.generate(session, panel, named)
    assert first.startswith("ali_vpn_") and second.startswith("ali_vpn_") and first != second
    persian = await factories.customer(session, env["shop"], telegram_id=71004)
    persian.username = None
    persian.first_name = "علی"  # a Persian first name, no @username
    assert (await usernames.generate(session, panel, persian)).startswith("ali_")
    nameless = await factories.customer(session, env["shop"], telegram_id=71005)
    nameless.username, nameless.first_name = None, None
    assert (await usernames.generate(session, panel, nameless)).startswith("71005_")


# --- OPB-2: selling only while the account can serve, alerts in both bots ------------------


async def _child_with_plan(session, env):  # type: ignore[no-untyped-def]
    child = await own_panel.connect(session, env["account"], TOKEN, verify=False)
    panel = await session.scalar(select(Panel).where(Panel.shop_id == child.id))
    plan = await factories.plan(session, child, panel, price=120_000, days=30, data_gb=30)
    buyer = await factories.customer(session, child, telegram_id=72001)
    return child, plan, buyer


async def test_a_full_panel_stops_sales_and_trials_and_renewal_reopens_them(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import orders, trials
    from paurubot.db.models import OrderKind

    child, plan, buyer = await _child_with_plan(session, env)
    await shop_settings.set_many(session, child.id, {"trial_enabled": True})
    account = env["account"]
    account.used_bytes = account.data_limit  # the account's traffic is used up
    assert await own_panel.recheck_all(session) == [(child, "own_paused_traffic_full")]
    with pytest.raises(orders.OrderError) as exc:
        await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    assert exc.value.key == "own_sales_paused"
    assert await trials.eligibility(session, buyer) == "own_sales_paused"
    assert await own_panel.recheck_all(session) == []  # announced once, not on every pass

    account.used_bytes = 0  # topped up
    assert await own_panel.recheck_all(session) == [(child, "own_resumed")]
    draft = await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    assert draft.base_price == 120_000
    assert await trials.eligibility(session, buyer) != "own_sales_paused"


async def test_a_full_user_capacity_stops_new_users_only(session, env) -> None:  # type: ignore[no-untyped-def]
    child, _, _ = await _child_with_plan(session, env)
    env["account"].users_count = env["account"].max_users
    assert await own_panel.recheck_all(session) == [(child, "own_paused_users_full")]
    assert await own_panel.sales_gate(session, child.id, new_user=True) == "own_sales_paused"
    assert await own_panel.sales_gate(session, child.id, new_user=False) is None  # renewals, extras


@pytest.mark.parametrize(("status", "reason"), [("expired", "expired"), ("paused", "inactive")])
async def test_an_ended_account_stops_everything(session, env, status, reason) -> None:  # type: ignore[no-untyped-def]
    child, _, _ = await _child_with_plan(session, env)
    env["account"].status = status
    assert await own_panel.recheck_all(session) == [(child, f"own_paused_{reason}")]
    assert await own_panel.sales_gate(session, child.id, new_user=False) == "own_sales_paused"


async def test_reopening_waits_for_a_live_login(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    child, _, _ = await _child_with_plan(session, env)
    env["account"].status = "expired"
    await own_panel.recheck_all(session)
    env["account"].status = "active"

    async def down(panel):  # type: ignore[no-untyped-def]
        raise own_panel.OwnPanelError("own_panel_unreachable")

    monkeypatch.setattr(own_panel, "check_login", down)
    assert await own_panel.recheck_all(session) == []  # looks fine, but cannot log in yet
    assert await own_panel.sales_gate(session, child.id, new_user=True) == "own_sales_paused"


async def test_a_manual_hold_pauses_whatever_the_account_says(session, env) -> None:  # type: ignore[no-untyped-def]
    child, _, _ = await _child_with_plan(session, env)
    await shop_settings.set_many(session, child.id, {own_panel.HOLD: True})
    assert await own_panel.sales_gate(session, child.id, new_user=False) == "own_sales_paused"


async def test_other_shops_are_never_gated(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {own_panel.PAUSED: "expired"})
    assert await own_panel.sales_gate(session, env["shop"].id, new_user=True) is None


async def test_alerts_reach_the_reseller_bot_and_the_main_shop(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.auth.staff import create_staff
    from paurubot.notifications import handlers  # noqa: F401
    from tests.unit.test_provisioning import drain

    child, _, _ = await _child_with_plan(session, env)
    await create_staff(
        session, env["shop"].id, role=StaffRole.OWNER, username=None, password=None, telegram_id=990001
    )
    env["account"].status = "expired"
    await own_panel.announce(session, await own_panel.recheck_all(session))
    await session.commit()
    env["tg"].calls.clear()
    await drain(session, "notify")
    sent = {(c.chat_id, c.text.split("\n")[0][:12]) for c in env["tg"].calls if hasattr(c, "text")}
    chats = {chat for chat, _ in sent}
    assert env["owner"].telegram_id in chats  # the reseller, through its own bot
    assert 990001 in chats  # the main shop's staff copy
    texts = [c.text for c in env["tg"].calls if hasattr(c, "text")]
    assert any("@ali_own_bot" in text for text in texts)
