"""Daily summary (FIN-04, JOB-02) and error monitoring redaction (NEW-05)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

import pytest

from paurubot.config import get_settings
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
    from paurubot.core.reports import daily_details
    from paurubot.worker import jobs

    details = await daily_details(session, shop.id, start, end)
    assert (details.purchases, details.renewals, details.total) == (1, 1, 300_000)
    assert details.yesterday_total == 150_000 and details.change_percent == 100
    assert details.best_hour == inside.astimezone(get_settings().tz).hour
    messages = jobs.daily_messages(Texts("fa"), "1405/07/14", asdict(summary), details)
    assert len(messages) == 3 and all("{" not in m for m in messages)
    assert "300,000" in messages[0] and "+100٪" in messages[0] and "150,000" in messages[1]
    assert panel.name in messages[2]
    # The shop picks which parts of the report to send (settings › daily report).
    only = jobs.daily_messages(Texts("fa"), "1405/07/14", asdict(summary), details, ["panels", "summary"])
    assert only == [messages[0], messages[2]]
    assert jobs.daily_messages(Texts("fa"), "1405/07/14", asdict(summary), details, []) == []


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


# --- REF-06: a missed report hour still sends the report --------------------------------------


@pytest.mark.parametrize(
    ("now", "hour", "day"),
    [
        ("2026-11-02T23:10", 23, "2026-11-02"),  # on time
        ("2026-11-03T02:30", 23, "2026-11-02"),  # the worker missed 23:00: yesterday's report, late
        ("2026-11-03T10:00", 23, "2026-11-02"),  # still yesterday's until 23:00 comes again
        ("2026-11-03T00:20", 0, "2026-11-02"),  # midnight now works (it used to read as 23)
        ("2026-11-03T05:59", 0, "2026-11-02"),
        ("2026-11-03T09:00", 9, "2026-11-03"),
        ("2026-11-03T08:59", 9, "2026-11-02"),
    ],
)
def test_report_day_is_due_from_its_hour_on(now: str, hour: int, day: str) -> None:
    from datetime import datetime

    from paurubot.config import get_settings
    from paurubot.core.reports import day_bounds, report_day

    local = datetime.fromisoformat(now).replace(tzinfo=get_settings().tz)
    start, _ = day_bounds(report_day(local, hour))
    assert start.date().isoformat() == day


async def test_a_missed_hour_sends_the_report_late_and_once(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot import kv
    from paurubot.config import get_settings
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import LogChannel
    from paurubot.notifications import telegram
    from paurubot.worker import jobs

    shop = await factories.shop(session)
    session.add(LogChannel(shop_id=shop.id, topic="reports", chat_id=-100777))
    hour = (utcnow().astimezone(get_settings().tz) - timedelta(hours=2)).hour  # two hours ago
    await shop_settings.set_many(session, shop.id, {"daily_report_hour": hour})
    await session.commit()
    sent: list[int] = []

    async def capture(chat_id, text, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(chat_id)

    monkeypatch.setattr(telegram, "send_text", capture)
    await kv.get_redis().delete(
        *[k async for k in kv.get_redis().scan_iter(f"report:daily:{shop.id}:*")] or ["-"]
    )
    await jobs.daily_report()
    await jobs.daily_report()  # the next 5-minute run: already sent
    assert sent.count(-100777) == 3  # summary, money, panels: once
