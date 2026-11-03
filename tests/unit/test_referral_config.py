"""A free config for every N counted invites (the referral reward config).

The invite reward used to be cash only: a share of the friend's purchase in the wallet. A shop can
now give a config instead or as well: every N invites (counted on the friend's purchase or on their
join) earn one config, from a plan or a size typed in settings.
"""

from __future__ import annotations

from sqlalchemy import func, select

from paurubot.core import customers, referral_rewards
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Job,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    ReferralClaim,
    Service,
)
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB
from paurubot.provisioning import rewards
from tests import factories
from tests.unit import test_service_lifecycle
from tests.unit.test_service_lifecycle import drain

env = test_service_lifecycle.env
isolate_queue = test_service_lifecycle.isolate_queue


async def _configure(session, shop, **values):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, shop.id, {"referral_enabled": True, **values})
    shop_settings.invalidate(shop.id)
    await session.commit()
    return await shop_settings.load(session, shop.id, fresh=True)


async def _friend(session, shop, referrer: Customer) -> Customer:  # type: ignore[no-untyped-def]
    friend = await factories.customer(session, shop)
    await customers.set_referrer(session, friend, referrer.telegram_id)
    return friend


async def _bought(session, shop, friend: Customer, amount: int = 100_000) -> Order:  # type: ignore[no-untyped-def]
    """A completed paid order of the friend, as provisioning would finish it."""
    order = Order(
        shop_id=shop.id,
        customer_id=friend.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        base_price=amount,
        amount=amount,
        paid_at=utcnow(),
        completed_at=utcnow(),
        params={},
    )
    session.add(order)
    await session.flush()
    await rewards.reward_referrer(session, order)
    await session.commit()
    return order


async def test_purchases_count_and_each_n_earn_one_config(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_every=3,
        referral_config_plan_id=plan.id,
        referral_min_order=50_000,
    )
    before = await wallet_balance(session, shop.id, referrer.id)
    friends = [await _friend(session, shop, referrer) for _ in range(4)]
    await _bought(session, shop, friends[0], amount=10_000)  # under the minimum: does not count
    for friend in friends[:3]:
        await _bought(session, shop, friend)
    await _bought(session, shop, friends[0])  # a second purchase never counts twice
    state = await referral_rewards.status(session, referrer, cfg)
    assert (state.counted, state.available, state.progress) == (3, 1, 0)
    # Config only: no cash share went to the wallet.
    assert await wallet_balance(session, shop.id, referrer.id) == before
    ready = await session.scalar(
        select(func.count(Job.id)).where(Job.kind == "referral_config_ready", Job.status == JobStatus.QUEUED)
    )
    assert ready == 1  # told once, when the third purchase completed the config

    order = await referral_rewards.claim(session, referrer, cfg)
    await session.commit()
    assert order.kind == OrderKind.PURCHASE and order.amount == 0 and order.source == "referral"
    assert order.plan_id == plan.id and order.status == OrderStatus.PAID
    state = await referral_rewards.status(session, referrer, cfg)
    assert (state.available, state.progress) == (0, 0)
    try:
        await referral_rewards.claim(session, referrer, cfg)
    except referral_rewards.RewardError as exc:
        assert exc.key == "referral_config_not_enough"
    else:
        raise AssertionError("a second config without more invites")
    await session.rollback()

    # The config is made and delivered like any purchase, on the plan's size.
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    service = await session.get(Service, order.service_id)
    assert (
        service is not None and service.customer_id == referrer.id and service.data_limit == plan.data_gb * GB
    )


async def test_a_failed_config_gives_its_invites_back(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    cfg = await _configure(
        session, shop, referral_reward_mode="config", referral_config_every=1, referral_config_plan_id=plan.id
    )
    await _bought(session, shop, await _friend(session, shop, referrer))
    order = await referral_rewards.claim(session, referrer, cfg)
    await session.commit()
    assert (await referral_rewards.status(session, referrer, cfg)).available == 0
    order.status = OrderStatus.FAILED  # the panel was down and provisioning gave up
    await session.commit()
    assert (await referral_rewards.status(session, referrer, cfg)).available == 1
    assert await session.scalar(select(func.count(ReferralClaim.id))) >= 1  # the claim stays on record


async def test_both_pays_cash_and_counts_toward_the_config(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="both",
        referral_percent=10,
        referral_config_every=2,
        referral_config_plan_id=plan.id,
    )
    before = await wallet_balance(session, shop.id, referrer.id)
    await _bought(session, shop, await _friend(session, shop, referrer), amount=200_000)
    assert await wallet_balance(session, shop.id, referrer.id) == before + 20_000
    assert (await referral_rewards.status(session, referrer, cfg)).counted == 1


async def test_cash_mode_is_unchanged_and_shows_no_config(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, referrer = env["shop"], env["cust"]
    cfg = await _configure(session, shop, referral_percent=10)
    assert cfg["referral_reward_mode"] == "cash"  # the default keeps the old behavior
    before = await wallet_balance(session, shop.id, referrer.id)
    await _bought(session, shop, await _friend(session, shop, referrer), amount=200_000)
    assert await wallet_balance(session, shop.id, referrer.id) == before + 20_000
    try:
        await referral_rewards.claim(session, referrer, cfg)
    except referral_rewards.RewardError as exc:
        assert exc.key == "referral_config_off"
    else:
        raise AssertionError("claimed a config with the config reward off")


async def test_joining_counts_when_the_shop_says_so_and_banned_friends_never(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, referrer = env["shop"], env["cust"]
    panel = env["panel"]
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_counts="join",
        referral_config_every=2,
        referral_config_source="custom",
        referral_config_gb=5,
        referral_config_days=10,
        referral_config_panel_id=panel.id,
    )
    friends = [await _friend(session, shop, referrer) for _ in range(3)]
    friends[2].is_banned = True
    await session.commit()
    state = await referral_rewards.status(session, referrer, cfg)
    assert (state.counted, state.available) == (2, 1)
    order = await referral_rewards.claim(session, referrer, cfg)
    await session.commit()
    assert order.plan_id is None
    assert order.params["custom"] == {"gb": 5, "days": 10} and order.params["panel_id"] == panel.id
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    service = await session.get(Service, order.service_id)
    assert service is not None and service.data_limit == 5 * GB


async def test_a_reward_that_is_not_set_up_cannot_be_claimed(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, referrer = env["shop"], env["cust"]
    other = await factories.shop(session)
    foreign_panel = await factories.panel(session, other)
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_counts="join",
        referral_config_every=1,
        referral_config_source="custom",
        referral_config_panel_id=None,
    )
    await _friend(session, shop, referrer)
    await session.commit()
    for broken in ({}, {"referral_config_panel_id": foreign_panel.id}):
        cfg = {**cfg, **broken}
        assert await referral_rewards.reward(session, shop.id, cfg) is None
        try:
            await referral_rewards.claim(session, referrer, cfg)
        except referral_rewards.RewardError as exc:
            assert exc.key == "referral_config_unavailable"
        else:
            raise AssertionError("claimed a config with no usable reward")


async def test_settings_are_checked(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    other = await factories.shop(session)
    other_panel = await factories.panel(session, other)
    other_plan = await factories.plan(session, other, other_panel)
    await session.commit()
    for bad, key in (
        ({"referral_reward_mode": "points"}, "referral_reward_mode_invalid"),
        ({"referral_config_counts": "click"}, "referral_config_counts_invalid"),
        ({"referral_config_source": "stock"}, "referral_config_source_invalid"),
        ({"referral_config_every": 0}, "referral_config_every_invalid"),
        ({"referral_config_gb": 10_001}, "referral_config_gb_invalid"),
        ({"referral_config_days": "30"}, "referral_config_days_invalid"),
        ({"referral_config_plan_id": other_plan.id}, "referral_config_plan_id_invalid"),
        ({"referral_config_panel_id": other_panel.id}, "referral_config_panel_id_invalid"),
    ):
        try:
            await referral_rewards.check_settings(session, shop.id, bad)
        except referral_rewards.RewardError as exc:
            assert exc.key == key
        else:
            raise AssertionError(bad)
    await referral_rewards.check_settings(
        session,
        shop.id,
        {
            "referral_reward_mode": "both",
            "referral_config_plan_id": env["plan"].id,
            "referral_config_every": 5,
        },
    )


async def test_the_mini_app_shows_progress_and_claims(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from paurubot.api import webapp_auth
    from paurubot.api.app import create_app
    from tests.unit.test_webapp_api import h

    shop, plan = env["shop"], env["plan"]

    async def our_shop(_session):  # type: ignore[no-untyped-def]
        return shop

    monkeypatch.setattr(webapp_auth, "root_shop", our_shop)
    await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_counts="join",
        referral_config_every=2,
        referral_config_plan_id=plan.id,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        me = (await client.get("/api/webapp/me", headers=h(99101))).json()
        referral = me["referral"]
        assert referral["cash"] is False and referral["config"]["every"] == 2
        assert (
            referral["config"]["available"] == 0 and referral["config"]["reward"]["plan_title"] == plan.title
        )
        refused = await client.post("/api/webapp/referral/claim", headers=h(99101))
        assert refused.status_code == 409 and refused.json()["error"] == "referral_config_not_enough"
        referrer = await session.scalar(select(Customer).where(Customer.telegram_id == 99101))
        for _ in range(2):
            await _friend(session, shop, referrer)
        await session.commit()
        me = (await client.get("/api/webapp/me", headers=h(99101))).json()
        assert me["referral"]["config"]["available"] == 1
        taken = await client.post("/api/webapp/referral/claim", headers=h(99101))
        assert taken.status_code == 200, taken.text
        assert taken.json()["config"]["available"] == 0
        order = await session.get(Order, taken.json()["order_id"])
        assert order is not None and order.customer_id == referrer.id and order.source == "referral"
        assert (await client.post("/api/webapp/referral/claim", headers=h(99101))).status_code == 409
    await _configure(session, shop, referral_reward_mode="cash")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        me = (await client.get("/api/webapp/me", headers=h(99101))).json()
        assert me["referral"]["cash"] is True and me["referral"]["config"] is None


async def test_the_bot_screen_shows_the_rule_progress_and_a_button(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.bot.routers.account import referral_screen
    from paurubot.i18n import Texts

    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    t = Texts("en")
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="both",
        referral_config_counts="join",
        referral_config_every=2,
        referral_config_plan_id=plan.id,
    )
    await _friend(session, shop, referrer)
    await session.commit()
    text, markup = await referral_screen(session, referrer, t, cfg, "https://t.me/x?start=ref_1")
    assert t("referral_config_progress", progress=1, every=2) in text
    assert t("referral_cash_line", percent=cfg["referral_percent"]) in text and markup is None
    await _friend(session, shop, referrer)
    await session.commit()
    text, markup = await referral_screen(session, referrer, t, cfg, "https://t.me/x?start=ref_1")
    assert t("referral_config_available", available=1) in text
    assert markup is not None and markup.inline_keyboard[0][0].text == t("btn_referral_claim")
    cash_only = await _configure(session, shop, referral_reward_mode="cash")
    text, markup = await referral_screen(session, referrer, t, cash_only, "https://t.me/x?start=ref_1")
    assert text == t(
        "referral_text", link="https://t.me/x?start=ref_1", count=2, percent=cash_only["referral_percent"]
    )


async def test_a_double_tap_takes_one_config_even_with_two_ready(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_counts="join",
        referral_config_every=1,
        referral_config_plan_id=plan.id,
    )
    for _ in range(2):
        await _friend(session, shop, referrer)
    await session.commit()
    assert (await referral_rewards.status(session, referrer, cfg)).available == 2
    await referral_rewards.claim(session, referrer, cfg)
    await session.commit()
    try:
        await referral_rewards.claim(session, referrer, cfg)
    except referral_rewards.RewardError as exc:
        assert exc.key == "referral_config_wait"
    else:
        raise AssertionError("a double tap took two configs")
    await session.rollback()
    await session.refresh(referrer)
    assert (await referral_rewards.status(session, referrer, cfg)).available == 1  # the second is still there


async def test_a_purchase_deleted_for_a_refund_no_longer_counts(session, env) -> None:  # type: ignore[no-untyped-def]
    """Buying and at once deleting the service for a refund must not farm invites."""
    from paurubot.core import cancellation, orders
    from paurubot.ledger import wallet

    shop, plan, referrer = env["shop"], env["plan"], env["cust"]
    cfg = await _configure(
        session,
        shop,
        referral_reward_mode="config",
        referral_config_every=1,
        referral_config_plan_id=plan.id,
        self_delete_enabled=True,
        self_delete_refund_percent=100,
    )
    friend = await _friend(session, shop, referrer)
    await wallet.topup(session, shop.id, friend.id, 100_000, provider="card", payment_id=friend.id + 10_000)
    order = await orders.create(
        session, friend, await orders.draft(session, friend, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    assert (await referral_rewards.status(session, referrer, cfg)).available == 1

    service = await session.get(Service, order.service_id, populate_existing=True)
    assert service is not None
    await session.refresh(friend)
    refund = await cancellation.cancel(session, friend, service)
    await session.commit()
    assert refund > 0
    assert (await referral_rewards.status(session, referrer, cfg)).available == 0
