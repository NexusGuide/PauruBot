"""Auto-renewal of panel admin accounts from the wallet (TEN-14)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from paurubot.core import account_auto_renew
from paurubot.db.base import utcnow
from paurubot.db.models import Job, Order, OrderKind
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB
from tests.unit.test_panel_accounts import _bought, _buyer, env, isolate_queue  # noqa: F401  (fixtures)
from tests.unit.test_resellers import drain


async def test_it_renews_from_the_wallet_when_the_period_ends(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    await account_auto_renew.enable(session, account, {})
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "idle"  # 30 days left
    end = utcnow() + timedelta(minutes=30)
    account.expire_at = end
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "placed"
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) in {"cooldown", "busy"}
    await drain(session, "provision")
    await session.refresh(account)
    assert abs((account.expire_at - end).total_seconds() - 30 * 86400) < 5
    order = await session.scalar(select(Order).where(Order.source == "auto"))
    assert order is not None and order.kind == OrderKind.ACCOUNT_RENEW
    assert await wallet_balance(session, env["shop"].id, cust.id) == 3_000_000


async def test_traffic_running_out_also_renews_with_the_chosen_package(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    plan = env["plan"]
    plan.renew_packages_enabled = True
    plan.renew_packages = [{"days": 10, "gb": 100, "price": 200_000}]
    await session.commit()
    cust = await _buyer(session, env["shop"])
    _order, account = await _bought(session, env, cust)
    assert account_auto_renew.choices(account) == [{"package": 0}]  # packages replace the plain renewal
    with pytest.raises(account_auto_renew.AutoRenewError):
        await account_auto_renew.enable(session, account, {})
    await account_auto_renew.enable(session, account, {"package": 0})
    account.used_bytes = 499 * GB + 900 * 1024**2
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "placed"
    order = await session.scalar(select(Order).where(Order.source == "auto"))
    assert order is not None and order.amount == 200_000 and order.params["renew"]["package"] == 0


async def test_a_short_wallet_is_told_once_and_a_gone_option_stops_it(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    cust = await _buyer(session, env["shop"], balance=1_000_000)
    _order, account = await _bought(session, env, cust)  # the wallet is now empty
    await account_auto_renew.enable(session, account, {})
    account.expire_at = utcnow() + timedelta(minutes=10)
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "short"
    assert await account_auto_renew.run_account(session, account.id) == "short"
    notes = list(await session.scalars(select(Job).where(Job.kind == "account_auto_renew")))
    assert [n.payload["reason"] for n in notes] == ["insufficient_balance"]
    await wallet.topup(session, env["shop"].id, cust.id, 1_000_000, provider="card", payment_id=9_999)
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "placed"
    await session.commit()
    await drain(session, "provision")
    # The plan stops being renewable: the rule stops and the owner is told.
    env["plan"].renewable = False
    account.expire_at = utcnow() + timedelta(minutes=10)
    account.auto_renew = {}
    await session.commit()
    assert await account_auto_renew.run_account(session, account.id) == "stopped"
    assert account.auto_renew["stopped"] == "renew_option_unavailable"
    assert not account_auto_renew.is_on(account)
