"""Daily summary (FIN-04, JOB-02) and error monitoring redaction (NEW-05)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from paurubot.core.reports import daily_summary, day_bounds
from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus, Payment, PaymentPurpose, PaymentStatus
from paurubot.i18n import Texts
from tests import factories


async def test_daily_summary_counts_only_that_day(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=150_000)
    customer = await factories.customer(session, shop)
    start, end = day_bounds(utcnow())
    inside = start + timedelta(hours=3)
    for kind, when in [
        (OrderKind.PURCHASE, inside),
        (OrderKind.RENEW, inside),
        (OrderKind.PURCHASE, start - timedelta(minutes=1)),
    ]:
        session.add(
            Order(
                shop_id=shop.id,
                customer_id=customer.id,
                kind=kind,
                status=OrderStatus.COMPLETED,
                plan_id=plan.id,
                base_price=plan.price,
                amount=plan.price,
                completed_at=when,
            )
        )
    session.add(
        Payment(
            shop_id=shop.id,
            customer_id=customer.id,
            purpose=PaymentPurpose.TOPUP,
            provider="card",
            status=PaymentStatus.VERIFYING,
            amount=50_000,
            idempotency_key="report-test",
        )
    )
    await session.flush()
    summary = await daily_summary(session, shop.id, start, end)
    assert summary.orders == 2
    assert summary.revenue == 300_000
    assert summary.renewals == 1
    assert summary.pending_receipts == 1
    text = Texts("fa")("daily_report", date="1405/07/14", **asdict(summary))
    assert "{" not in text and "300000" in text


def test_day_bounds_are_one_local_day() -> None:
    start, end = day_bounds(utcnow())
    assert end - start == timedelta(days=1)
    assert start.hour == 0 and start.minute == 0


def test_sentry_events_are_redacted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types

    from pydantic import SecretStr

    from paurubot.config import get_settings
    from paurubot.logs import setup_error_monitoring

    captured: dict = {}
    fake = types.SimpleNamespace(init=lambda **kwargs: captured.update(kwargs))
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(get_settings(), "sentry_dsn", SecretStr("https://key@sentry.example/1"))
    assert setup_error_monitoring("worker") is True
    assert captured["send_default_pii"] is False
    event = {"message": "login failed password=hunter22 for 123456789:" + "A" * 35}
    scrubbed = captured["before_send"](event, {})
    assert "hunter22" not in scrubbed["message"] and "AAAA" not in scrubbed["message"]


def test_no_dsn_means_no_monitoring(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from paurubot.config import get_settings
    from paurubot.logs import setup_error_monitoring

    monkeypatch.setattr(get_settings(), "sentry_dsn", SecretStr(""))
    assert setup_error_monitoring("api") is False
