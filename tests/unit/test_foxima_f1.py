"""Foxima parity, batch F1: referral anti-abuse, per-method payment rules, and switching all of a
customer's services off and on. Every option is off by default; nothing of ours changes."""

from __future__ import annotations

from datetime import timedelta

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.provisioning.rewards import reward_referrer
from tests import factories
from tests.unit.test_sales_extras import _paid


async def test_an_invite_counts_only_with_a_verified_phone_and_an_old_enough_account(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {"referral_percent": 10, "referral_require_phone": True, "referral_min_account_hours": 24},
    )
    inviter = await factories.customer(session, shop)
    friend = await factories.customer(session, shop)
    friend.referrer_id = inviter.id
    await session.flush()
    assert await reward_referrer(session, await _paid(session, shop, friend, 100_000)) == 0  # no phone
    friend.phone = "+989121234567"
    assert await reward_referrer(session, await _paid(session, shop, friend, 100_000)) == 0  # too new
    friend.created_at = utcnow() - timedelta(hours=25)
    await session.flush()
    assert await reward_referrer(session, await _paid(session, shop, friend, 100_000)) == 10_000
    # Off by default: the same friend in a shop without the rules is rewarded at once.
    other = await factories.shop(session)
    await shop_settings.set_many(session, other.id, {"referral_percent": 10})
    boss = await factories.customer(session, other)
    newbie = await factories.customer(session, other)
    newbie.referrer_id = boss.id
    await session.flush()
    assert await reward_referrer(session, await _paid(session, other, newbie, 100_000)) == 10_000


async def test_each_payment_method_can_have_its_own_minimum_maximum_and_help(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from paurubot.payments import engine
    from paurubot.payments.base import PaymentError
    from tests.unit.test_payments import _zarinpal, _zarinpal_shop

    provider, _seen = _zarinpal()
    shop = await _zarinpal_shop(session, monkeypatch, provider)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "payment_method_rules": [
                {"provider": "zarinpal", "min": 100_000, "max": 500_000, "help": "Pay with any Shetab card."}
            ]
        },
    )
    cust = await factories.customer(session, shop)
    for amount in (80_000, 600_000):
        with pytest.raises(PaymentError) as exc:
            await engine.start(session, cust, "zarinpal", amount)
        assert exc.value.key == "amount_out_of_range"
        assert exc.value.params == {"low": 100_000, "high": 500_000}
    started = await engine.start(session, cust, "zarinpal", 200_000)
    assert started.payment.amount == 200_000
    assert await engine.method_help(session, shop.id, "zarinpal") == "Pay with any Shetab card."
    assert await engine.method_help(session, shop.id, "card") == ""
    for bad in (
        [{"provider": "zarinpal", "min": 500, "max": 100}],
        [{"provider": "nope", "min": 1}],
        [{"provider": "zarinpal", "help": "x" * 2001}],
    ):
        with pytest.raises(shop_settings.SettingError):
            await shop_settings.set_many(session, shop.id, {"payment_method_rules": bad})


def test_the_panel_lists_exactly_the_servers_payment_methods() -> None:
    import re
    from pathlib import Path

    from paurubot.payments.engine import PROVIDERS

    source = Path(__file__).parents[2] / "frontend/src/admin/screens/FoximaTools.tsx"
    listed = re.search(r"export const METHODS = \[([^\]]+)\]", source.read_text()).group(1)  # type: ignore[union-attr]
    assert re.findall(r'"([a-z0-9_]+)"', listed) == list(PROVIDERS)
