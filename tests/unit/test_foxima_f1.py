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


async def test_the_lucky_wheel_can_be_a_telegram_dice_or_slot(session) -> None:  # type: ignore[no-untyped-def]
    """Foxima's wheel is Telegram's own dice: a six (or a slot jackpot) wins a set wallet prize."""
    import pytest

    from paurubot.core import growth
    from paurubot.ledger.accounts import wallet_balance

    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "wheel_enabled": True,
            "wheel_buyers_only": False,
            "wheel_style": "dice",
            "wheel_dice_prize": 25_000,
        },
    )
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    lucky = await factories.customer(session, shop)
    unlucky = await factories.customer(session, shop)

    async def six() -> int:
        return 6

    async def three() -> int:
        return 3

    won = await growth.dice_spin(session, lucky, cfg, roll=six)
    assert won.prize["kind"] == "wallet" and won.prize["value"] == 25_000
    assert await wallet_balance(session, shop.id, lucky.id) == 25_000
    lost = await growth.dice_spin(session, unlucky, cfg, roll=three)
    assert lost.prize["kind"] == "nothing"
    with pytest.raises(growth.GrowthError) as exc:  # one try per period, as the wheel
        await growth.dice_spin(session, lucky, cfg, roll=six)
    assert exc.value.key == "wheel_wait"
    assert growth.dice_wins("slot", 64) and growth.dice_wins("slot", 22) and not growth.dice_wins("slot", 5)
    assert growth.dice_wins("dice", 6) and not growth.dice_wins("dice", 5)


async def test_remnawave_users_are_tagged_by_how_many_services_their_buyer_has(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Foxima's Remnawave loyalty: GOLD from 5 active services, SILVER from 3, BRONZE from 1."""
    from paurubot.core import remnawave_loyalty
    from paurubot.db.models import PanelKind, Service, ServiceStatus
    from paurubot.panels import UserSpec, driver_for, registry
    from tests.fakes.panels import FakeRemnawavePanel, transport

    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.REMNAWAVE)
    fake = FakeRemnawavePanel()
    registry.test_transports[panel.id] = transport(fake)
    try:
        driver = await driver_for(panel)
        big, small = await factories.customer(session, shop), await factories.customer(session, shop)
        services = []
        for owner, count in ((big, 3), (small, 1)):
            for n in range(count):
                made = await driver.create_user(UserSpec(username=f"rw_{owner.id}_{n}", data_limit=1024**3))
                row = Service(
                    shop_id=shop.id,
                    customer_id=owner.id,
                    panel_id=panel.id,
                    panel_username=made.username,
                    panel_data=made.data,
                    status=ServiceStatus.ACTIVE,
                )
                session.add(row)
                services.append(row)
        await session.commit()
        assert await remnawave_loyalty.run(session) == 0  # off by default
        await shop_settings.set_many(session, shop.id, {"remnawave_loyalty_tags": True})
        await session.commit()
        monkeypatch.setattr(remnawave_loyalty, "BATCH", 3)  # more services than one batch
        assert await remnawave_loyalty.run(session) == 4
        tags = sorted(str(u.get("tag")) for u in fake.users.values())
        assert tags == ["BRONZE", "SILVER", "SILVER", "SILVER"]
        before = getattr(fake, "patches", 0)
        assert await remnawave_loyalty.run(session) == 0  # nothing changed, nothing sent again
        assert getattr(fake, "patches", 0) == before
        assert remnawave_loyalty.tier_for(5) == "GOLD" and remnawave_loyalty.tier_for(0) is None
    finally:
        registry.test_transports.clear()
        registry._cache.clear()
