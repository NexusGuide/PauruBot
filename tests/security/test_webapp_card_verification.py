"""Mini App card proof never grants approval or bypasses payment policy."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.api.routes import webapp_cards
from paurubot.core import cards
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Payment, Shop, VerifiedCard
from paurubot.worker import queue
from tests import factories
from tests.fakes.telegram import png_bytes
from tests.security import test_payment_intents as concurrent_fixtures
from tests.unit import test_webapp_api as fixtures

pytestmark = pytest.mark.security
app_env = fixtures.app_env
h = fixtures.h
intent_engine = concurrent_fixtures.intent_engine
PATH = "/api/webapp/cards/verification"
NUMBER = "4242424242424242"


def body(**changes):  # type: ignore[no-untyped-def]
    return {
        "intent_key": "card-client-key-0001",
        "number": NUMBER,
        "image": base64.b64encode(png_bytes()).decode(),
        **changes,
    }


async def enable(session, env):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, env["shop"].id, {"card_verify_enabled": True, "card_verify_min_amount": 0}
    )
    await session.commit()


async def test_owned_replay_review_and_no_implicit_payment(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    await enable(session, app_env)
    store = AsyncMock(return_value="telegram-photo-id")
    notify = AsyncMock(wraps=queue.enqueue)
    monkeypatch.setattr(webapp_cards, "store_photo", store)
    monkeypatch.setattr(queue, "enqueue", notify)
    assert (await client.get(PATH)).status_code == 401
    assert (await client.post(PATH, json=body())).status_code == 401
    first = await client.post(PATH, headers=h(98301), json=body())
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["card"]["status"] == "pending"
    assert set(first.json()["card"]) == {"id", "masked", "status"}
    repeated = await client.post(PATH, headers=h(98301), json=body())
    assert repeated.json() == first.json()
    conflict = await client.post(PATH, headers=h(98301), json=body(number="4000000000000002"))
    assert conflict.status_code == 409 and conflict.json()["error"] == "card_verification_conflict"
    pending = await client.post(PATH, headers=h(98301), json=body(intent_key="card-client-key-0002"))
    assert pending.status_code == 409 and pending.json()["error"] == "card_verify_pending"
    assert (await client.get(PATH, headers=h(98302))).json()["items"] == []
    state = (await client.get(PATH, headers=h(98301))).json()
    assert not state["can_submit"] and not state["approved"]
    for secret in (NUMBER, "telegram-photo-id", "fingerprint", "digest", "other_customer_id"):
        assert secret not in str(state)
    # Pending proof is NOT payment authorization. Existing payment engine remains authoritative.
    pay = await client.post(
        "/api/webapp/wallet/topup",
        headers=h(98301),
        json={"amount": 100_000, "method": "card", "intent_key": "verify-payment-intent-1"},
    )
    assert pay.status_code == 400 and pay.json()["error"] == "card_verify_required"
    row = await session.get(VerifiedCard, first.json()["card"]["id"])
    customer = await session.get(Customer, row.customer_id)
    assert await cards.needs_verification(session, customer, 100_000)
    assert await cards.review(session, row, True, None)
    await session.commit()
    approved = (await client.get(PATH, headers=h(98301))).json()
    assert approved["approved"] and not approved["can_submit"]
    assert not await cards.needs_verification(session, customer, 100_000)
    # Same key after staff review returns that same reviewed row, without another Telegram call/job.
    repeated = await client.post(PATH, headers=h(98301), json=body())
    assert repeated.json()["card"]["id"] == row.id and repeated.json()["card"]["status"] == "approved"
    assert await session.scalar(select(func.count(Payment.id))) == 0
    store.assert_awaited_once()
    assert notify.await_count == 1
    assert notify.call_args.args[1:3] == ("notify", "card_verify")


@pytest.mark.parametrize(
    "changes",
    [
        {"customer_id": 1},
        {"shop_id": 1},
        {"status": "approved"},
        {"number": 4242424242424242},
        {"number": "x" + NUMBER},
        {"number": "۴۲۴۲۴۲۴۲۴۲۴۲۴۲۴۱"},
        {"intent_key": "short"},
        {"image": "<svg/>"},
    ],
    ids=["customer", "shop", "approval", "number-type", "number-letters", "luhn", "key", "image"],
)
async def test_validation_never_echoes_sensitive_body(session, app_env, monkeypatch, changes) -> None:  # type: ignore[no-untyped-def]
    await enable(session, app_env)
    store = AsyncMock(side_effect=AssertionError("invalid input reached Telegram"))
    monkeypatch.setattr(webapp_cards, "store_photo", store)
    result = await app_env["client"].post(PATH, headers=h(98303), json=body(**changes))
    assert result.status_code == 422, result.text
    assert NUMBER not in result.text and "image" not in result.text
    assert await session.scalar(select(func.count(VerifiedCard.id))) == 0
    store.assert_not_awaited()


async def test_rejected_proof_can_be_resubmitted_explicitly_and_receipts_cannot_approve_it(
    session, app_env, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    await enable(session, app_env)
    monkeypatch.setattr(webapp_cards, "store_photo", AsyncMock(return_value="safe-photo"))
    client = app_env["client"]
    first = (await client.post(PATH, headers=h(98304), json=body())).json()["card"]
    row = await session.get(VerifiedCard, first["id"])
    assert await cards.review(session, row, False, None)
    await session.commit()
    repeat = await client.post(PATH, headers=h(98304), json=body())
    assert repeat.json()["card"]["status"] == "rejected"
    again = await client.post(PATH, headers=h(98304), json=body(intent_key="card-client-new-0002"))
    assert again.status_code == 200 and again.json()["card"]["id"] != row.id
    assert not (await client.get(PATH, headers=h(98304))).json()["approved"]
    receipt = await client.post(
        f"/api/webapp/payments/{row.id}/receipt",
        headers=h(98304),
        files={"file": ("receipt.png", png_bytes(), "image/png")},
    )
    assert receipt.status_code == 404
    assert not (await client.get(PATH, headers=h(98304))).json()["approved"]


async def test_disabled_blocked_trusted_and_storage_failure_do_not_register(
    session, app_env, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    store = AsyncMock(side_effect=webapp_cards.HTTPException(503, "card_photo_unavailable"))
    monkeypatch.setattr(webapp_cards, "store_photo", store)
    assert (await client.post(PATH, headers=h(98305), json=body())).status_code == 409
    store.assert_not_awaited()
    await enable(session, app_env)
    own = (await client.get("/api/webapp/me", headers=h(98305))).json()
    customer = await session.get(Customer, own["id"])
    for trust, status in (("blocked", 403), ("trusted", 409)):
        customer.card_trust = trust
        await session.commit()
        assert not (await client.get(PATH, headers=h(98305))).json()["can_submit"]
        assert (await client.post(PATH, headers=h(98305), json=body())).status_code == status
    store.assert_not_awaited()
    customer.card_trust = "normal"
    await session.commit()
    assert (await client.post(PATH, headers=h(98305), json=body())).status_code == 503
    assert await session.scalar(select(func.count(VerifiedCard.id))) == 0


async def test_inconsistent_foreign_shop_rows_never_approve_or_list(session, app_env) -> None:  # type: ignore[no-untyped-def]
    await enable(session, app_env)
    client = app_env["client"]
    own = (await client.get("/api/webapp/me", headers=h(98306))).json()
    foreign = await factories.shop(session)
    session.add(
        VerifiedCard(
            shop_id=foreign.id,
            customer_id=own["id"],
            last4="4242",
            masked="4242-42**-****-4242",
            digest="foreign-digest",
            status="approved",
        )
    )
    await session.commit()
    state = (await client.get(PATH, headers=h(98306))).json()
    assert not state["approved"] and state["can_submit"] and state["items"] == []


async def test_bot_and_miniapp_core_share_one_pending_lock(intent_engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    async with maker() as session:
        shop = Shop(title="Card concurrency")
        session.add(shop)
        await session.flush()
        customer = await factories.customer(session, shop)
        cid = customer.id
        await session.commit()
    barrier = asyncio.Barrier(4)

    async def register():  # type: ignore[no-untyped-def]
        async with maker() as session:
            customer = await session.get(Customer, cid)
            await barrier.wait()
            try:
                card, _ = await cards.register(session, customer, NUMBER, "fake-photo")
                await session.commit()
                return card.id
            except cards.CardError as exc:
                await session.rollback()
                return exc.key

    results = await asyncio.wait_for(asyncio.gather(*(register() for _ in range(4))), timeout=20)
    assert sum(isinstance(result, int) for result in results) == 1
    assert results.count("card_verify_pending") == 3
    async with maker() as session:
        assert (
            await session.scalar(select(func.count(VerifiedCard.id)).where(VerifiedCard.customer_id == cid))
            == 1
        )
