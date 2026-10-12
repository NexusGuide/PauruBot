"""Extra features a reseller bot may offer when staff allow them (TEN-18)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.core import orders, resale, trials
from paurubot.core import settings as shop_settings
from paurubot.db.models import Order, OrderKind, OrderStatus, Panel, Service
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from tests import factories
from tests.unit.test_provisioning import drain
from tests.unit.test_reseller_bot import TOKEN, _sell
from tests.unit.test_reseller_bot import env as env  # noqa: F401  (fixture)


async def _child(session, env, *features: str):  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    for name in features:
        await resale.allow_feature(session, env["profile"], name, True)
    await session.commit()
    if features:
        await resale.update(session, child, {resale.FEATURES[name]: True for name in features})
        await session.commit()
    shop_settings.invalidate(child.id)
    return child


async def test_reseller_turns_on_only_what_staff_allow(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await _child(session, env)
    with pytest.raises(resale.ResaleError, match="resale_feature_not_allowed"):
        await resale.update(session, child, {"trial_enabled": True})
    await resale.update(session, child, {"trial_enabled": False})  # turning off is always fine
    await resale.allow_feature(session, env["profile"], "trial", True)
    await resale.update(session, child, {"trial_enabled": True})
    await session.commit()
    assert resale.allowed_features(env["profile"]) == {"trial"}
    assert (await shop_settings.load(session, child.id, fresh=True))["trial_enabled"]

    await resale.allow_feature(session, env["profile"], "trial", False)  # staff take it back
    await session.commit()
    assert not (await shop_settings.load(session, child.id, fresh=True))["trial_enabled"]
    with pytest.raises(resale.ResaleError):
        resale.set_feature_right(env["profile"], "wheel", True)


async def test_trial_in_a_reseller_bot_is_paid_by_the_reseller(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    await shop_settings.set_many(
        session, shop.id, {"resale_trial_fee": 3_000, "trial_gb": 2, "trial_days": 1}
    )
    child = await _child(session, env)
    buyer = await factories.customer(session, child, telegram_id=63001)
    with pytest.raises(trials.TrialError, match="trial_disabled"):
        await trials.request(session, buyer)  # not allowed yet

    await resale.allow_feature(session, env["profile"], "trial", True)
    await resale.update(session, child, {"trial_enabled": True})
    await session.commit()
    shop_settings.invalidate(child.id)
    shop_settings.invalidate(shop.id)
    order = await trials.request(session, buyer)
    await session.commit()
    assert order.params["wholesale"] == 3_000 and order.params["gb"] == 2
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    service = await session.get(Service, order.service_id)
    assert service is not None and service.shop_id == child.id and service.is_trial
    assert await wallet_balance(session, shop.id, env["reseller"].id) == 197_000


async def test_no_trial_without_the_fee_in_the_wallet(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    await shop_settings.set_many(session, shop.id, {"resale_trial_fee": 300_000})
    child = await _child(session, env, "trial")
    shop_settings.invalidate(shop.id)
    buyer = await factories.customer(session, child, telegram_id=63002)
    with pytest.raises(trials.TrialError, match="trial_unavailable"):
        await trials.request(session, buyer)
    assert await session.scalar(select(Order).where(Order.customer_id == buyer.id)) is None


async def test_extras_in_a_reseller_bot_carry_the_resellers_price(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    await shop_settings.set_many(session, shop.id, {"extra_gb_price": 5_000, "extra_day_price": 2_000})
    child = await _child(session, env)
    shop_settings.invalidate(shop.id)
    buyer, order = await _sell(session, child, plan, 63003, 96_000)
    service = await session.get(Service, order.service_id)
    assert service is not None
    with pytest.raises(orders.OrderError, match="extras_unavailable"):
        await orders.draft(session, buyer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=5)

    await resale.allow_feature(session, env["profile"], "extras", True)
    await resale.update(session, child, {"extras_enabled": True})
    await session.commit()
    shop_settings.invalidate(child.id)
    sale = await resale.for_shop(session, child.id)
    assert sale is not None
    assert await resale.extra_units(session, sale, service) == (4_000, 1_600)  # 20% off the shop's
    cfg = await shop_settings.load(session, child.id)
    assert await resale.listed_extras(session, service, child.id, cfg) == (4_800, 1_900)  # plus 20%
    d = await orders.draft(session, buyer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=5)
    assert d.base_price == 24_000 and d.params["wholesale"] == 20_000
    before = await wallet_balance(session, shop.id, env["reseller"].id)
    await wallet.topup(session, child.id, buyer.id, 24_000, provider="card", payment_id=63013)
    extra = await orders.create(session, buyer, d)
    await orders.settle_from_wallet(session, extra.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(extra)
    assert extra.status == OrderStatus.COMPLETED
    assert await wallet_balance(session, shop.id, env["reseller"].id) == before - 20_000


async def test_upgrades_credit_both_prices(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    panel = await session.get(Panel, plan.panel_id)
    bigger = await factories.plan(session, shop, panel, price=200_000, days=30, data_gb=100, title="Big")
    await shop_settings.set_many(session, shop.id, {"upgrade_enabled": True})
    child = await _child(session, env)
    shop_settings.invalidate(shop.id)
    buyer, order = await _sell(session, child, plan, 63004, 96_000)
    service = await session.get(Service, order.service_id)
    assert service is not None
    with pytest.raises(orders.OrderError):
        await orders.draft(session, buyer, OrderKind.UPGRADE, plan=bigger, service=service)
    await resale.allow_feature(session, env["profile"], "upgrade", True)
    await resale.update(session, child, {"upgrade_enabled": True})
    await session.commit()
    shop_settings.invalidate(child.id)
    d = await orders.draft(session, buyer, OrderKind.UPGRADE, plan=bigger, service=service)
    # A fresh 30-day service: almost the whole old price is credited on both sides.
    assert 80_000 <= d.params["wholesale"] <= 81_000  # 160,000 - ~80,000
    assert 96_000 <= d.base_price <= 97_000  # 192,000 - ~96,000
    assert d.base_price >= d.params["wholesale"]


async def test_parent_trial_policy_cannot_be_bypassed(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    await shop_settings.set_many(session, shop.id, {"trial_enabled": False})
    child = await _child(session, env, "trial")
    buyer = await factories.customer(session, child, telegram_id=63005)
    with pytest.raises(trials.TrialError, match="trial_disabled"):
        await trials.request(session, buyer)

    await shop_settings.set_many(
        session, shop.id, {"trial_enabled": True, "trial_requires_phone": True}
    )
    shop_settings.invalidate(shop.id)
    with pytest.raises(trials.TrialError, match="trial_needs_phone"):
        await trials.request(session, buyer)
