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
