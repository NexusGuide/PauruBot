"""F-15: independent preference, tenant isolation and send-time suppression."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select, update

from paurubot.core import broadcasts, marketing
from paurubot.db.models import AuditLog, Broadcast, Customer, JobStatus
from paurubot.notifications import handlers
from tests import factories


async def test_explicit_choice_is_audited_once_and_does_not_mute_service_alerts(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    assert customer.marketing_opt_out is False and customer.marketing_changed_at is None
    assert customer.alerts_muted is False
    first = await marketing.set_preference(session, customer, opt_out=True, source="bot")
    await session.commit()
    again = await marketing.set_preference(session, customer, opt_out=True, source="miniapp")
    assert first == again and customer.marketing_change_source == "bot"
    assert not await marketing.allowed(session, customer)
    assert customer.alerts_muted is False
    count = await session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.entity_id == customer.id, AuditLog.action == "customer.marketing_preference"
        )
    )
    assert count == 1
    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == customer.id, AuditLog.action == "customer.marketing_preference"
        )
    )
    assert entry.actor_type == "customer" and entry.actor_id == customer.id and entry.shop_id == shop.id
    await marketing.set_preference(session, customer, opt_out=False, source="miniapp")
    assert await marketing.allowed(session, customer)


async def test_same_telegram_account_has_independent_shop_preferences(session) -> None:  # type: ignore[no-untyped-def]
    root = await factories.shop(session)
    child = await factories.shop(session)
    child.parent_id = root.id
    first = await factories.customer(session, root, telegram_id=915001)
    second = await factories.customer(session, child, telegram_id=915001)
    await marketing.set_preference(session, first, opt_out=True, source="bot")
    assert not await marketing.allowed(session, first)
    assert await marketing.allowed(session, second)


async def test_audience_filters_ads_but_not_wallet_gift_eligibility(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    opted_out = await factories.customer(session, shop)
    opted_out.marketing_opt_out = True
    eligible = await factories.customer(session, shop)
    eligible.locale = "en"
    banned = await factories.customer(session, shop)
    banned.is_banned = True
    blocked = await factories.customer(session, shop)
    blocked.blocked_bot_at = handlers.utcnow()
    guest = await factories.customer(session, shop, telegram_id=-(10**15 + 915))
    await session.flush()
    row = Broadcast(shop_id=shop.id, audience={"kind": "never_bought", "locale": "en"})
    ids = list(await session.scalars(select(Customer.id).where(*handlers._audience(row))))
    assert ids == [eligible.id]
    gifts = list(await session.scalars(select(Customer.id).where(*handlers.audience_conditions(shop.id))))
    assert opted_out.id in gifts and eligible.id in gifts
    assert banned.id not in gifts and blocked.id not in gifts and guest.id not in gifts


async def test_worker_rechecks_opt_out_after_loading_a_batch(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    first = await factories.customer(session, shop)
    second = await factories.customer(session, shop)
    already_out = await factories.customer(session, shop)
    already_out.marketing_opt_out = True
    row = Broadcast(shop_id=shop.id, text="campaign", audience={}, run=0)
    session.add(row)
    await session.commit()
    sent = []

    def copier(_bot, _row):  # type: ignore[no-untyped-def]
        def to(chat_id):  # type: ignore[no-untyped-def]
            async def send():  # type: ignore[no-untyped-def]
                sent.append(chat_id)
                # Keep the loaded ORM customer stale, like a concurrent preference commit.
                await session.execute(
                    update(Customer)
                    .where(Customer.id == second.id)
                    .values(marketing_opt_out=True)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                return None

            return send

        return to

    async def deliver(call):  # type: ignore[no-untyped-def]
        return await call()

    monkeypatch.setattr(handlers.telegram, "bot_for", AsyncMock(return_value=object()))
    monkeypatch.setattr(handlers.telegram, "deliver", deliver)
    monkeypatch.setattr(handlers, "broadcast_copier", copier)
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())
    await handlers.run_broadcast(session, SimpleNamespace(payload={"broadcast_id": row.id, "run": 0}))
    assert sent == [first.telegram_id]
    assert row.status == JobStatus.DONE and row.sent == 1 and row.failed == 0 and row.skipped == 1
    assert row.cursor == second.id and broadcasts.progress(row)["left"] == 0


async def test_private_discount_offer_is_suppressed_but_wallet_notice_is_not(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    await marketing.set_preference(session, customer, opt_out=True, source="bot")
    texts = AsyncMock(return_value=lambda key, **kw: key)
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_texts", texts)
    monkeypatch.setattr(handlers, "_to_customer", send)
    await handlers.simple_customer(
        session, SimpleNamespace(kind="private_code", payload={"customer_id": customer.id, "code": "OFFER"})
    )
    texts.assert_not_awaited()
    send.assert_not_awaited()
    await handlers.simple_customer(
        session, SimpleNamespace(kind="wallet_adjusted", payload={"customer_id": customer.id, "delta": 100})
    )
    send.assert_awaited_once()


async def test_explicit_initial_enable_records_choice_without_making_it_a_toggle(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    first = await marketing.set_preference(session, customer, opt_out=False, source="bot")
    assert first["changed_at"] is not None and first["marketing_opt_out"] is False
    assert await marketing.set_preference(session, customer, opt_out=False, source="bot") == first
