"""Competitor-gap backends: one-buy plans, transfers, notes, rotation, cards, lottery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paurubot.core import cards, growth, orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Service,
    ServiceStatus,
)
from tests import factories


async def _service(session, shop, customer, panel, plan=None):  # type: ignore[no-untyped-def]
    row = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id if plan else None,
        panel_username="u-gap-1",
        status=ServiceStatus.ACTIVE,
        expire_at=datetime.now(UTC) + timedelta(days=10),
    )
    session.add(row)
    await session.flush()
    return row


async def test_one_buy_refuses_second_purchase(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    plan.one_buy = True
    cust = await factories.customer(session, shop)
    session.add(
        Order(
            shop_id=shop.id,
            customer_id=cust.id,
            kind=OrderKind.PURCHASE,
            status=OrderStatus.COMPLETED,
            plan_id=plan.id,
            amount=plan.price,
        )
    )
    await session.flush()
    with pytest.raises(orders.OrderError, match="one_buy_already_owned"):
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    # A plan without the flag still drafts fine.
    plan.one_buy = False
    await session.flush()
    draft = await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert draft.plan is not None and draft.plan.id == plan.id


async def test_transfer_service_keeps_username(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    src = await factories.customer(session, shop)
    dst = await factories.customer(session, shop)
    service = await _service(session, shop, src, panel, plan)
    moved = await orders.transfer_service(session, service.id, src.id, dst.id)
    assert moved.customer_id == dst.id and moved.panel_username == "u-gap-1"
    assert moved.panel_data["transfers"][-1]["to"] == dst.id
    with pytest.raises(orders.OrderError, match="service_not_found"):
        await orders.transfer_service(session, service.id, src.id, dst.id)


async def test_transfer_service_respects_one_buy(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    plan.one_buy = True
    src = await factories.customer(session, shop)
    dst = await factories.customer(session, shop)
    service = await _service(session, shop, src, panel, plan)
    session.add(
        Order(
            shop_id=shop.id,
            customer_id=dst.id,
            kind=OrderKind.PURCHASE,
            status=OrderStatus.COMPLETED,
            plan_id=plan.id,
            amount=plan.price,
        )
    )
    await session.flush()
    with pytest.raises(orders.OrderError, match="one_buy_already_owned"):
        await orders.transfer_service(session, service.id, src.id, dst.id)


async def test_purchase_note_truncated_to_150(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    cust = await factories.customer(session, shop)
    draft = await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan, note="x" * 200)
    assert draft.params["customer_note"] == "x" * 150
    order = await orders.create(session, cust, draft, note="y" * 200)
    assert order.params["customer_note"] == "y" * 150


async def test_rotate_subscription_changes_token(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    cust = await factories.customer(session, shop)
    service = await _service(session, shop, cust, panel)
    first = await orders.rotate_subscription(session, service.id)
    assert first and service.sub_token == first
    second = await orders.rotate_subscription(session, service.id)
    assert second and second != first


async def test_card_first_pay_gate_and_pv_target(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "card_accounts": [{"number": "6037991234567893"}],
            "card_first_pay_only": True,
            "card_pv_mode": True,
            "card_pv_username": "support_pv",
        },
    )
    cust = await factories.customer(session, shop)
    gated = await cards.visible_card_options(session, shop.id, cust.id)
    assert gated["cards"] == [] and gated["gated"] is True
    assert gated["pv_username"] == "support_pv" and gated["pv_mode"] is True
    session.add(
        Payment(
            shop_id=shop.id,
            customer_id=cust.id,
            purpose=PaymentPurpose.TOPUP,
            provider="card",
            status=PaymentStatus.PAID,
            amount=100_000,
            idempotency_key=f"gap-card-{cust.id}",
        )
    )
    await session.flush()
    shown = await cards.visible_card_options(session, shop.id, cust.id)
    assert len(shown["cards"]) == 1 and shown["gated"] is False


def test_score_top3_orders_and_ties() -> None:
    assert growth.score_top3_winners({1: 10, 2: 30, 3: 20, 4: 5}, 3) == [2, 3, 1]
    # Ties break by the lower customer id for a stable result.
    assert growth.score_top3_winners({7: 10, 5: 10, 9: 10}, 2) == [5, 7]
    assert growth.filter_lottery_pool({1: 2, 2: 3}, {2}, include_agents=False) == {1: 2}
    assert growth.filter_lottery_pool({1: 2, 2: 3}, {2}, include_agents=True) == {1: 2, 2: 3}
