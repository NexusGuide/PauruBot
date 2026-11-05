"""Selling PasarGuard admin accounts (TEN-14): buying, renewing, traffic and user slots, alerts
and expiry, all through ordinary orders and the provisioning worker."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from paurubot.core import orders, panel_accounts
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AccountPlan,
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelAccount,
    PanelKind,
    ResellerGroup,
    ResellerProfile,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_resellers import drain


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    fake = FakeTokenPanel("pasarguard")
    registry.test_transports[panel.id] = transport(fake)
    plan = AccountPlan(
        shop_id=shop.id,
        panel_id=panel.id,
        title="Starter",
        role_id=2,
        data_gb=500,
        max_users=50,
        days=30,
        price=1_000_000,
        gb_price=1_000,
        user_price=5_000,
    )
    session.add(plan)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _buyer(session, shop, balance=5_000_000):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, balance, provider="card", payment_id=cust.id)
    await session.commit()
    return cust


async def _pay(session, cust, kind, **kw):  # type: ignore[no-untyped-def]
    draft = await panel_accounts.draft(session, cust, kind, **kw)
    order = await orders.create(session, cust, draft)
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    return order


async def _bought(session, env, cust):  # type: ignore[no-untyped-def]
    order = await _pay(session, cust, OrderKind.ACCOUNT_PURCHASE, plan=env["plan"])
    account = await session.get(PanelAccount, int(order.params["account_id"]))
    assert account is not None
    return order, account


async def test_buying_creates_the_admin_with_its_role_quota_and_user_cap(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    cust = await _buyer(session, env["shop"])
    order, account = await _bought(session, env, cust)
    sent = fake.sold_admins[account.username]
    assert sent["role_id"] == 2 and sent["data_limit"] == 500 * GB
    assert sent["permission_overrides"]["max_users"] == 50
    assert sent["telegram_id"] == cust.telegram_id
    assert sent["password"] == panel_accounts.password_of(account)
    assert account.username.startswith(f"r{cust.telegram_id}_")
    assert account.status == "active" and account.max_users == 50
    assert account.expire_at is not None and account.expire_at > utcnow() + timedelta(days=29)
    assert order.amount == 1_000_000
    assert await wallet_balance(session, env["shop"].id, cust.id) == 4_000_000
    # The buyer is told, with the login.
    ready = await session.scalar(select(Job).where(Job.queue == "notify", Job.kind == "account_ready"))
    assert ready is not None and ready.payload["account_id"] == account.id
    assert await panel_accounts.accounts_of(session, cust) == [account]


async def test_a_retry_after_a_failed_attempt_keeps_the_same_admin(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    cust = await _buyer(session, env["shop"])
    draft = await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_PURCHASE, plan=env["plan"])
    order = await orders.create(session, cust, draft)
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    fake.fail_next = 502
    await drain(session, "provision")  # the first attempt fails and is scheduled again
    await session.refresh(order)
    username = order.params["username"]
    await session.execute(update(Job).where(Job.queue == "provision").values(run_at=utcnow()))
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    assert list(fake.sold_admins) == [username]


async def test_reseller_discounts_never_apply_to_admin_accounts(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    cust = await _buyer(session, shop)
    group = ResellerGroup(shop_id=shop.id, title="Gold", discount_percent=30)
    session.add(group)
    await session.flush()
    session.add(ResellerProfile(shop_id=shop.id, customer_id=cust.id, group_id=group.id))
    await session.flush()
    await session.refresh(cust, ["reseller_profile"])
    draft = await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_PURCHASE, plan=env["plan"])
    order = await orders.create(session, cust, draft)
    assert (order.amount, order.reseller_discount) == (1_000_000, 0)


async def test_switched_off_resellers_only_and_reseller_bots(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    cust = await _buyer(session, shop)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_resellers_only": True})
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_PURCHASE, plan=plan)
    assert exc.value.key == "accounts_unavailable"
    await shop_settings.set_many(
        session, shop.id, {"panel_accounts_resellers_only": False, "panel_accounts_enabled": False}
    )
    assert not await panel_accounts.available(session, cust)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    assert await panel_accounts.available(session, cust)
    other = await factories.shop(session, "Other")
    stranger = await factories.customer(session, other)
    await shop_settings.set_many(session, other.id, {"panel_accounts_enabled": True})
    with pytest.raises(orders.OrderError) as exc:  # a plan of another shop
        await panel_accounts.draft(session, stranger, OrderKind.ACCOUNT_PURCHASE, plan=plan)
    assert exc.value.key == "plan_unavailable"


async def test_renew_add_traffic_and_users(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    before = account.expire_at
    assert before is not None
    fake.sold_admins[account.username]["used_traffic"] = 100 * GB
    order = await _pay(session, cust, OrderKind.ACCOUNT_GB, account=account, amount=50)
    assert order.amount == 50_000
    await session.refresh(account)
    assert account.data_limit == 550 * GB
    assert fake.sold_admins[account.username]["data_limit"] == 550 * GB
    order = await _pay(session, cust, OrderKind.ACCOUNT_USERS, account=account, amount=10)
    assert order.amount == 50_000
    await session.refresh(account)
    assert account.max_users == 60
    assert fake.sold_admins[account.username]["permission_overrides"]["max_users"] == 60
    await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    await session.refresh(account)
    assert account.expire_at is not None
    assert abs((account.expire_at - before).total_seconds() - 30 * 86400) < 5  # added to the period left
    assert account.data_limit == 500 * GB and account.max_users == 60  # paid slots are kept
    assert fake.sold_admins[account.username]["used_traffic"] == 0
    with pytest.raises(orders.OrderError):
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_GB, account=account, amount=0)
    stranger = await _buyer(session, env["shop"])
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, stranger, OrderKind.ACCOUNT_RENEW, account=account)
    assert exc.value.key == "account_not_found"


async def test_extra_days_lengthen_the_period_within_the_shops_range(session, env) -> None:  # type: ignore[no-untyped-def]
    plan = env["plan"]
    plan.day_price = 2_000
    await shop_settings.set_many(
        session, env["shop"].id, {"panel_accounts_days_min": 5, "panel_accounts_days_max": 60}
    )
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    before = account.expire_at
    assert before is not None
    order = await _pay(session, cust, OrderKind.ACCOUNT_DAYS, account=account, amount=10)
    assert order.amount == 20_000 and order.params["days"] == 10
    await session.refresh(account)
    assert account.expire_at is not None
    assert abs((account.expire_at - before).total_seconds() - 10 * 86400) < 5  # added to the period left
    for amount in (4, 61):
        with pytest.raises(orders.OrderError) as exc:
            await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_DAYS, account=account, amount=amount)
        assert exc.value.key == "acc_amount_range"
        assert exc.value.params == {"min_value": 5, "max_value": 60}
    # Not sold: no price on the plan, or a plan that never ends.
    plan.day_price = 0
    await session.commit()
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_DAYS, account=account, amount=10)
    assert exc.value.key == "extras_unavailable"
    plan.day_price = 2_000
    account.expire_at = None
    await session.commit()
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_DAYS, account=account, amount=10)
    assert exc.value.key == "extras_unavailable"


async def test_extra_traffic_follows_the_shops_range(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, env["shop"].id, {"panel_accounts_gb_min": 20, "panel_accounts_gb_max": 300}
    )
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    for amount in (10, 301):
        with pytest.raises(orders.OrderError) as exc:
            await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_GB, account=account, amount=amount)
        assert exc.value.key == "acc_amount_range"
    order = await _pay(session, cust, OrderKind.ACCOUNT_GB, account=account, amount=300)
    assert order.amount == 300_000
    assert await panel_accounts.extra_bounds(session, env["shop"].id) == {"gb": (20, 300), "days": (1, 365)}


async def test_days_paid_just_before_the_end_switch_the_account_back_on(session, env) -> None:  # type: ignore[no-untyped-def]
    """The period ran out between paying and the worker applying it: the days bring it back."""
    fake = env["fake"]
    env["plan"].day_price = 1_000
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    draft = await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_DAYS, account=account, amount=7)
    order = await orders.create(session, cust, draft)
    await orders.settle_from_wallet(session, order.id)
    account.expire_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    await panel_accounts.run(session)  # the expiry pass switches it off first
    await session.refresh(account)
    assert account.status == "expired"
    await drain(session, "provision")
    await session.refresh(account)
    assert account.status == "active"
    assert account.expire_at is not None and account.expire_at > utcnow() + timedelta(days=6)
    assert fake.sold_admins[account.username]["status"] == "active"
    assert (account.username, "activate") in fake.admin_user_actions


async def test_renewal_packages_and_a_custom_renewal_per_plan(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    plan = env["plan"]
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    plan.renew_packages_enabled = True
    plan.renew_packages = [
        {"days": 60, "gb": 1000, "price": 1_700_000},
        {"days": 15, "gb": 0, "price": 300_000},
    ]
    await session.commit()
    # The plan's own renewal is not offered once packages are.
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    assert exc.value.key == "renew_option_unavailable"
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_RENEW, account=account, package=2)
    assert exc.value.key == "renew_option_unavailable"
    before = account.expire_at
    assert before is not None
    fake.sold_admins[account.username]["used_traffic"] = 300 * GB
    order = await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account, package=0)
    assert order.amount == 1_700_000 and order.params["renew"] == {"days": 60, "gb": 1000, "package": 0}
    await session.refresh(account)
    assert account.expire_at is not None
    assert abs((account.expire_at - before).total_seconds() - 60 * 86400) < 5
    assert account.data_limit == 1000 * GB and fake.sold_admins[account.username]["data_limit"] == 1000 * GB
    assert fake.sold_admins[account.username]["used_traffic"] == 0
    # A paid renewal keeps what it bought even if the plan changes before the worker runs.
    draft = await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_RENEW, account=account, package=1)
    pending = await orders.create(session, cust, draft)
    await orders.settle_from_wallet(session, pending.id)
    plan.renew_packages = [{"days": 1, "gb": 1, "price": 1}]
    await session.commit()
    await drain(session, "provision")
    await session.refresh(account)
    assert account.data_limit == 0  # the 15-day package has no quota

    # A custom renewal sized by the buyer, in this plan's own range, priced per day and GB.
    plan.renew_custom_enabled = True
    plan.day_price, plan.gb_price = 2_000, 1_000
    plan.days_min, plan.days_max, plan.gb_min, plan.gb_max = 10, 90, 100, 2000
    await shop_settings.set_many(
        session, env["shop"].id, {"panel_accounts_days_min": 1, "panel_accounts_days_max": 5}
    )
    await session.commit()
    for days, gb in ((9, 500), (91, 500), (30, 99), (30, 2001)):
        with pytest.raises(orders.OrderError) as exc:
            await panel_accounts.draft(
                session, cust, OrderKind.ACCOUNT_RENEW, account=account, renew_days=days, renew_gb=gb
            )
        assert exc.value.key == "acc_amount_range", (days, gb)
    before = account.expire_at
    order = await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account, renew_days=30, renew_gb=800)
    assert order.amount == 30 * 2_000 + 800 * 1_000
    await session.refresh(account)
    assert abs((account.expire_at - before).total_seconds() - 30 * 86400) < 5
    assert account.data_limit == 800 * GB
    # The plan's range wins over the shop's for its extras too.
    assert await panel_accounts.extra_bounds(session, env["shop"].id, plan) == {
        "gb": (100, 2000),
        "days": (10, 90),
    }
    plan.renew_custom_enabled = False
    await session.commit()
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(
            session, cust, OrderKind.ACCOUNT_RENEW, account=account, renew_days=30, renew_gb=800
        )
    assert exc.value.key == "renew_option_unavailable"


def test_renewal_packages_are_validated() -> None:
    good = [{"days": 30, "gb": 0, "price": 0}]
    assert panel_accounts.clean_packages(good) == good
    for bad in (
        "x",
        [{"days": 0, "gb": 1, "price": 1}],
        [{"days": 30, "gb": -1, "price": 1}],
        [{"days": 30, "gb": 1, "price": True}],
        [{"days": 30, "gb": 1}],
        [{"days": 30, "gb": 1, "price": 1}] * 13,
    ):
        with pytest.raises(ValueError):
            panel_accounts.clean_packages(bad)


@pytest.mark.parametrize(
    ("mode", "days_from_end", "limit_gb", "usage_reset"),
    [
        (None, True, 500, True),  # the original rule: time added, a fresh quota
        ("extend_reset", True, 500, True),
        ("extend_carry", True, 1000, False),  # the whole old quota stays, usage keeps counting
        ("restart_reset", False, 500, True),
        ("restart_carry", False, 800, True),  # 300 GB left + 500 new, usage reset
    ],
)
async def test_the_plans_renewal_rule_decides_time_and_traffic(  # type: ignore[no-untyped-def]
    session, env, mode, days_from_end, limit_gb, usage_reset
) -> None:
    fake, plan = env["fake"], env["plan"]
    plan.renew_mode = mode
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    end = account.expire_at
    assert end is not None
    fake.sold_admins[account.username]["used_traffic"] = 200 * GB  # 300 of 500 GB left
    order = await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    await session.refresh(account)
    assert account.expire_at is not None
    if days_from_end:
        assert abs((account.expire_at - end).total_seconds() - 30 * 86400) < 5
    else:
        assert abs((account.expire_at - utcnow()).total_seconds() - 30 * 86400) < 60
    assert account.data_limit == limit_gb * GB
    assert fake.sold_admins[account.username]["data_limit"] == limit_gb * GB
    assert (fake.sold_admins[account.username]["used_traffic"] == 0) is usage_reset
    assert order.params["reset_usage"] is usage_reset


async def test_an_ended_account_always_starts_fresh(session, env) -> None:  # type: ignore[no-untyped-def]
    fake, plan = env["fake"], env["plan"]
    plan.renew_mode = "extend_carry"
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    fake.sold_admins[account.username]["used_traffic"] = 100 * GB
    account.expire_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    await panel_accounts.run(session)
    await session.refresh(account)
    assert account.status == "expired"
    await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    await session.refresh(account)
    assert account.data_limit == 500 * GB  # not 400 left + 500
    assert fake.sold_admins[account.username]["used_traffic"] == 0
    assert abs((account.expire_at - utcnow()).total_seconds() - 30 * 86400) < 60


async def test_who_may_buy_a_plan_and_whether_it_renews(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import PlanAudience

    plan = env["plan"]
    plan.audience = PlanAudience.RESELLERS
    await session.commit()
    cust = await _buyer(session, env["shop"])
    assert await panel_accounts.plans(session, env["shop"].id, cust) == []
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_PURCHASE, plan=plan)
    assert exc.value.key == "plan_unavailable"
    plan.audience = PlanAudience.CUSTOMERS
    await session.commit()
    assert await panel_accounts.plans(session, env["shop"].id, cust) == [plan]
    _order, account = await _bought(session, env, cust)
    plan.renewable = False
    await session.commit()
    with pytest.raises(orders.OrderError) as exc:
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    assert exc.value.key == "account_not_renewable"


async def test_quota_alert_once_and_expiry_switches_off_until_renewed(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    fake.sold_admins[account.username].update({"used_traffic": 460 * GB, "total_users": 7})
    assert await panel_accounts.run(session) == 1
    await session.refresh(account)
    assert account.users_count == 7 and account.used_bytes == 460 * GB
    alerts = list(await session.scalars(select(Job).where(Job.kind == "account_alert")))
    assert [j.payload["alert"] for j in alerts] == ["account_alert_quota"]
    await panel_accounts.run(session)
    assert len(list(await session.scalars(select(Job).where(Job.kind == "account_alert")))) == 1
    account.expire_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    await panel_accounts.run(session)
    await session.refresh(account)
    assert account.status == "expired"
    assert fake.sold_admins[account.username]["status"] == "disabled"
    assert (account.username, "disable") in fake.admin_user_actions
    with pytest.raises(orders.OrderError) as exc:  # an ended account is renewed, not topped up
        await panel_accounts.draft(session, cust, OrderKind.ACCOUNT_GB, account=account, amount=10)
    assert exc.value.key == "account_not_active"
    await _pay(session, cust, OrderKind.ACCOUNT_RENEW, account=account)
    await session.refresh(account)
    assert account.status == "active" and account.expire_at > utcnow() + timedelta(days=29)
    assert fake.sold_admins[account.username]["status"] == "active"
    assert (account.username, "activate") in fake.admin_user_actions


async def test_owner_password_and_pause(session, env) -> None:  # type: ignore[no-untyped-def]
    fake = env["fake"]
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    old = panel_accounts.password_of(account)
    new = await panel_accounts.change_password(session, account)
    assert new != old and panel_accounts.password_of(account) == new
    assert fake.sold_admins[account.username]["password"] == new
    await panel_accounts.set_paused(session, account, True)
    assert account.status == "paused" and fake.sold_admins[account.username]["status"] == "disabled"
    await panel_accounts.set_paused(session, account, False)
    assert account.status == "active" and fake.sold_admins[account.username]["status"] == "active"
    assert panel_accounts.login_url(account) == "https://panel.example.com/dashboard/"


def test_generated_passwords_pass_pasarguard_rules() -> None:
    for _ in range(200):
        p = panel_accounts.new_password()
        assert len(p) >= 12 and '"' not in p
        assert sum(c.isupper() for c in p) >= 2 and sum(c.islower() for c in p) >= 2
        assert sum(c.isdigit() for c in p) >= 2 and any(not c.isalnum() for c in p)
