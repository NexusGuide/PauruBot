from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from paurubot.payments import saved_checkout

NOW = datetime(2026, 10, 19, tzinfo=UTC)


@pytest.mark.parametrize("marker", ["order_attempt_v1", "hosted_attempt_v1"])
@pytest.mark.parametrize("status", ["created", "pending", "expired", "failed", "paid"])
def test_uncertain_order_attempt_has_review_action_without_instructions(marker: str, status: str) -> None:
    row = SimpleNamespace(
        id=1,
        shop_id=2,
        customer_id=3,
        order_id=4,
        provider="plisio",
        status=status,
        amount=90_000,
        expires_at=NOW + timedelta(minutes=10),
        provider_ref="known",
        meta={
            marker: {"state": "uncertain"},
            "checkout_intent_v1": {
                "request": {"provider": "plisio", "order_id": 4, "order_amount": 90_000},
                "checkout": {"provider_ref": "known", "redirect_url": "https://invoice.invalid/order"},
            },
        },
    )
    assert saved_checkout.uncertain(row)
    assert saved_checkout.action(row, NOW) == "review"
    assert saved_checkout.saved_payment(row, NOW) is None


def test_ready_order_attempt_can_resume_without_external_creation() -> None:
    row = SimpleNamespace(
        id=1,
        shop_id=2,
        customer_id=3,
        order_id=4,
        provider="plisio",
        status="pending",
        amount=90_000,
        expires_at=NOW + timedelta(minutes=10),
        provider_ref="known",
        meta={
            "order_attempt_v1": {"state": "ready"},
            "checkout_intent_v1": {
                "request": {"provider": "plisio", "order_id": 4, "order_amount": 90_000},
                "checkout": {"provider_ref": "known", "redirect_url": "https://invoice.invalid/order"},
            },
        },
    )
    order = SimpleNamespace(id=4, shop_id=2, customer_id=3, status="pending", expires_at=row.expires_at)
    assert saved_checkout.action(row, NOW, order) == "resume"
    assert saved_checkout.saved_payment(row, NOW, order) == {
        "payment_id": 1,
        "provider": "plisio",
        "amount": 90_000,
        "redirect_url": "https://invoice.invalid/order",
    }
    assert saved_checkout.saved_payment(row, NOW, None) is None
