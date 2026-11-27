"""Unit tests for Section 5: growth, discounts for panel admin accounts, referrals, and tickets."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from paurubot.core import customers, gifts, growth, orders, pricing, referral, tickets
from paurubot.db.base import utcnow
from paurubot.db.models import DiscountKind, TicketStatus


def _customer(**kw):
    defaults = dict(
        id=10,
        shop_id=1,
        telegram_id=10010,
        referrer_id=None,
        is_banned=False,
        display_name="Alice",
        first_name="Alice",
        username="alice",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _code(**kw):
    defaults = dict(
        id=5,
        shop_id=1,
        code="PROMO10",
        kind=DiscountKind.PERCENT,
        value=10,
        max_discount=None,
        min_amount=0,
        max_uses=None,
        used_count=0,
        per_customer_limit=0,
        plan_ids=[],
        account_plan_ids=[],
        customer_id=None,
        first_purchase_only=False,
        sticky=False,
        starts_at=None,
        expires_at=None,
        is_active=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_discount_account_plan_targeting():
    session = AsyncMock()
    cust = _customer()

    # Code restricted to panel admin account plan #7 only
    code = _code(plan_ids=[], account_plan_ids=[7])
    asyncio.run(pricing.validate_code(session, code, cust, plan_id=None, account_plan_id=7, amount=100_000))

    with pytest.raises(pricing.DiscountError) as exc:
        asyncio.run(
            pricing.validate_code(session, code, cust, plan_id=None, account_plan_id=8, amount=100_000)
        )
    assert exc.value.key == "discount_wrong_plan"

    with pytest.raises(pricing.DiscountError) as exc2:
        asyncio.run(
            pricing.validate_code(session, code, cust, plan_id=3, account_plan_id=None, amount=100_000)
        )
    assert exc2.value.key == "discount_wrong_plan"

    # Unrestricted code (both lists empty) applies to both VPN plans and panel admin account plans
    open_code = _code(plan_ids=[], account_plan_ids=[])
    asyncio.run(
        pricing.validate_code(session, open_code, cust, plan_id=None, account_plan_id=7, amount=50_000)
    )
    asyncio.run(
        pricing.validate_code(session, open_code, cust, plan_id=3, account_plan_id=None, amount=50_000)
    )

    # Code restricted to VPN plan #3 only must reject panel admin account orders
    vpn_code = _code(plan_ids=[3], account_plan_ids=[])
    with pytest.raises(pricing.DiscountError) as exc3:
        asyncio.run(
            pricing.validate_code(session, vpn_code, cust, plan_id=None, account_plan_id=7, amount=50_000)
        )
    assert exc3.value.key == "discount_wrong_plan"


def test_release_holds_only_decrements_when_deleted():
    order = SimpleNamespace(id=99, discount_code_id=5)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=0))
    asyncio.run(orders.release_holds(session, order))
    assert session.execute.await_count == 1  # did NOT decrement used_count when nothing deleted

    session2 = AsyncMock()
    session2.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
    asyncio.run(orders.release_holds(session2, order))
    assert session2.execute.await_count == 2  # decremented used_count once


def test_set_referrer_rejects_banned_and_two_node_cycle():
    cust = _customer(id=10, telegram_id=10010, referrer_id=None)

    banned_ref = _customer(id=20, telegram_id=10020, is_banned=True)
    session = AsyncMock()
    with patch("paurubot.core.customers.by_telegram", AsyncMock(return_value=banned_ref)):
        res = asyncio.run(customers.set_referrer(session, cust, 10020))
    assert res is None
    assert cust.referrer_id is None

    cycle_ref = _customer(id=20, telegram_id=10020, referrer_id=10, is_banned=False)
    with patch("paurubot.core.customers.by_telegram", AsyncMock(return_value=cycle_ref)):
        res2 = asyncio.run(customers.set_referrer(session, cust, 10020))
    assert res2 is None
    assert cust.referrer_id is None


def test_gift_redeem_rejects_banned_and_uses_prior_purchase():
    banned = _customer(is_banned=True)
    session = AsyncMock()
    with pytest.raises(gifts.GiftError) as exc:
        asyncio.run(gifts.redeem(session, banned, "GIFT1234"))
    assert exc.value.key == "gift_invalid"


def test_growth_banned_cannot_spin_or_join():
    banned = _customer(is_banned=True)
    session = AsyncMock()
    with pytest.raises(growth.GrowthError):
        asyncio.run(growth._check_spin(session, banned, {"wheel_enabled": True}, lock=False))

    lot = SimpleNamespace(id=1, status="open", mode="join", ends_at=utcnow() + timedelta(hours=1))
    with pytest.raises(growth.GrowthError):
        asyncio.run(growth.join(session, lot, banned))


def test_ticket_reopen():
    session = AsyncMock()
    ticket = SimpleNamespace(id=4, status=TicketStatus.CLOSED, updated_at=None)
    changed = asyncio.run(tickets.reopen(session, ticket))
    assert changed is True
    assert ticket.status == TicketStatus.OPEN

    changed_again = asyncio.run(tickets.reopen(session, ticket))
    assert changed_again is False


def test_pending_start_roundtrip_and_onboarding():
    store: dict[str, str] = {}

    async def fake_set(key: str, value: str, *, ttl: int = 0) -> None:
        store[key] = value

    async def fake_get(key: str) -> str | None:
        return store.get(key)

    async def fake_delete(key: str) -> None:
        store.pop(key, None)

    with (
        patch("paurubot.cache.kv.set", AsyncMock(side_effect=fake_set)),
        patch("paurubot.cache.kv.get", AsyncMock(side_effect=fake_get)),
        patch("paurubot.cache.kv.delete", AsyncMock(side_effect=fake_delete)),
    ):
        asyncio.run(referral.remember_pending_start(1, 42, 777777))
        pending, ref_id = asyncio.run(referral.take_pending_start(1, 42))
        assert pending is True
        assert ref_id == 777777

        # Consuming a second time returns False, None
        pending2, ref_id2 = asyncio.run(referral.take_pending_start(1, 42))
        assert pending2 is False
        assert ref_id2 is None
