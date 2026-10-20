from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from paurubot.payments import saved_checkout

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def row(**changes: Any) -> Any:
    provider = changes.get("provider", "nowpayments")
    meta = {
        "checkout_intent_v1": {
            "request": {"provider": provider, "amount": 90_000, "currency": "IRT"},
            "checkout": {
                "redirect_url": "https://pay.example/invoice/known",
                "provider_ref": "known",
                "instructions": {},
            },
        }
    }
    return SimpleNamespace(
        **{
            **dict(
                id=3,
                customer_id=4,
                shop_id=5,
                amount=90_000,
                provider=provider,
                status="pending",
                order_id=None,
                expires_at=NOW + timedelta(minutes=15),
                provider_ref="known",
                meta=meta,
            ),
            **changes,
        }
    )


def order(**changes: Any) -> Any:
    return SimpleNamespace(
        **{
            **dict(id=8, customer_id=4, shop_id=5, status="pending", expires_at=NOW + timedelta(minutes=15)),
            **changes,
        }
    )


def test_saved_resume_keeps_id_amount_and_url_without_provider_calls() -> None:
    result = saved_checkout.saved_payment(row(), NOW)
    assert result == {
        "payment_id": 3,
        "provider": "nowpayments",
        "amount": 90_000,
        "redirect_url": "https://pay.example/invoice/known",
    }
    assert saved_checkout.action(row(), NOW) == "resume"


@pytest.mark.parametrize(
    "status", ["created", "verifying", "paid", "failed", "expired", "refunded", "disputed"]
)
def test_non_pending_has_no_checkout(status) -> None:  # type: ignore[no-untyped-def]
    payment = row(status=status)
    assert saved_checkout.saved_payment(payment, NOW) is None
    assert saved_checkout.action(payment, NOW) == "status"


def test_expired_row_is_status_only_without_changing_it() -> None:
    payment = row(expires_at=NOW)
    assert saved_checkout.saved_payment(payment, NOW) is None
    assert payment.status == "pending"


def test_uncertain_attempt_never_exposes_even_a_partially_saved_url() -> None:
    payment = row()
    payment.meta["hosted_attempt_v1"] = {"state": "uncertain"}
    assert saved_checkout.saved_payment(payment, NOW) is None
    assert saved_checkout.action(payment, NOW) == "review"


@pytest.mark.parametrize(
    "meta",
    [None, {}, [], {"checkout_intent_v1": "bad"}, {"checkout_intent_v1": {"request": {}, "checkout": {}}}],
)
def test_missing_or_corrupt_snapshot_has_no_instructions(meta) -> None:  # type: ignore[no-untyped-def]
    assert saved_checkout.saved_payment(row(meta=meta), NOW) is None


@pytest.mark.parametrize("mutation", ["provider", "ref"])
def test_snapshot_must_match_stored_payment(mutation) -> None:  # type: ignore[no-untyped-def]
    payment = row()
    if mutation == "provider":
        payment.meta["checkout_intent_v1"]["request"]["provider"] = "plisio"
    else:
        payment.meta["checkout_intent_v1"]["checkout"]["provider_ref"] = "other"
    assert saved_checkout.saved_payment(payment, NOW) is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        1,
        "",
        "http://pay.example/x",
        "javascript:alert(1)",
        "//pay.example/x",
        "https://u:p@pay.example/x",
        "https://pay.example:wrong/x",
        "https://[bad/x",
        "https://pay.example/white space",
        "https://pay.example/\nnewline",
        "https://pay.example\\@evil.example/",
    ],
)
def test_unsafe_redirect_is_never_returned(url) -> None:  # type: ignore[no-untyped-def]
    payment = row()
    payment.meta["checkout_intent_v1"]["checkout"]["redirect_url"] = url
    assert saved_checkout.saved_payment(payment, NOW) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"customer_id": 99},
        {"shop_id": 99},
        {"id": 9},
        {"status": "canceled"},
        {"status": "paid"},
        {"expires_at": NOW},
    ],
)
def test_order_scope_status_and_expiry_rechecked(changes) -> None:  # type: ignore[no-untyped-def]
    payment = row(order_id=8)
    assert saved_checkout.saved_payment(payment, NOW, order(**changes)) is None


def test_pending_order_allowed_but_missing_order_blocked() -> None:
    payment = row(order_id=8)
    assert saved_checkout.saved_payment(payment, NOW) is None
    assert saved_checkout.saved_payment(payment, NOW, order()) is not None


def test_card_replays_saved_accounts_not_current_shop_settings() -> None:
    payment = row(provider="card", provider_ref=None)
    snapshot = payment.meta["checkout_intent_v1"]["checkout"]
    snapshot["provider_ref"] = None
    snapshot["instructions"] = {"accounts": [{"number": "6037990000000000", "holder": "Original"}]}
    assert saved_checkout.saved_payment(payment, NOW)["accounts"] == [
        {"number": "6037990000000000", "holder": "Original", "bank": ""}
    ]
    snapshot["instructions"]["accounts"] = ["bad"]
    assert saved_checkout.saved_payment(payment, NOW) is None


def test_stars_never_creates_or_returns_a_new_telegram_link() -> None:
    assert saved_checkout.saved_payment(row(provider="stars"), NOW) is None


def test_crypto_uses_saved_units_and_address_without_requoting() -> None:
    payment = row(provider="trx")
    snapshot = payment.meta["checkout_intent_v1"]["checkout"]
    snapshot["instructions"] = {
        "network": "trx",
        "label": "Tron",
        "address": "Tknown",
        "amount": "1.23",
        "memo": None,
        "expires_at": None,
    }
    assert saved_checkout.saved_payment(payment, NOW)["crypto"]["amount"] == "1.23"
    snapshot["instructions"]["address"] = None
    assert saved_checkout.saved_payment(payment, NOW) is None
