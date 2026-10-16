"""F-02: services that renew or top themselves up from the wallet."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from paurubot.core import auto_renew, orders
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Job,
    Order,
    OrderKind,
    OrderStatus,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import handlers, telegram
from tests import factories
from tests.fakes.telegram import make_bot

GB = 1024**3


@pytest.fixture
def fake_bot() -> Iterator[Any]:
    bot, recorder = make_bot()
    telegram.set_bot(bot)
    yield recorder
    telegram.set_bot(None)


def _usage(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "auto_renew": {"mode": "renew", "plan_id": 1},
        "status": ServiceStatus.ACTIVE,
        "is_trial": False,
        "data_limit": 50 * GB,
        "used_bytes": 10 * GB,
        "expire_at": utcnow() + timedelta(days=10),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_nothing_is_due_while_data_and_time_are_left() -> None:
    assert auto_renew.wanted(_usage()) == []  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(auto_renew=None, used_bytes=50 * GB)) == []  # type: ignore[arg-type]
    stopped = {"mode": "renew", "plan_id": 1, "stopped": "plan_unavailable"}
    assert auto_renew.wanted(_usage(auto_renew=stopped, used_bytes=50 * GB)) == []  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(is_trial=True, used_bytes=50 * GB)) == []  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(status=ServiceStatus.DISABLED, used_bytes=50 * GB)) == []  # type: ignore[arg-type]


def test_renew_rule_fires_on_low_data_ending_time_or_limited() -> None:
    renew = [(OrderKind.RENEW, {})]
    assert auto_renew.wanted(_usage(used_bytes=50 * GB - 100)) == renew  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(expire_at=utcnow() + timedelta(minutes=20))) == renew  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(status=ServiceStatus.LIMITED)) == renew  # type: ignore[arg-type]
    assert auto_renew.wanted(_usage(status=ServiceStatus.EXPIRED)) == renew  # type: ignore[arg-type]
    unlimited = _usage(data_limit=0, used_bytes=900 * GB)
    assert auto_renew.wanted(unlimited) == []  # type: ignore[arg-type]


def test_extra_rule_buys_only_what_runs_low() -> None:
    rule = {"mode": "extra", "gb": 10, "days": 30}
    low = _usage(auto_renew=rule, used_bytes=50 * GB)
    assert auto_renew.wanted(low) == [(OrderKind.EXTRA_VOLUME, {"extra_gb": 10})]  # type: ignore[arg-type]
    late = _usage(auto_renew=rule, expire_at=utcnow() + timedelta(minutes=5))
    assert auto_renew.wanted(late) == [(OrderKind.EXTRA_DAYS, {"extra_days": 30})]  # type: ignore[arg-type]
    both = _usage(auto_renew=rule, used_bytes=50 * GB, expire_at=utcnow())
    assert [k for k, _ in auto_renew.wanted(both)] == [OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS]  # type: ignore[arg-type]
    only_gb = _usage(auto_renew={"mode": "extra", "gb": 5}, expire_at=utcnow())
    assert auto_renew.wanted(only_gb) == []  # type: ignore[arg-type]
    assert auto_renew.rule(_usage(auto_renew={"mode": "extra"})) is None  # type: ignore[arg-type]


async def _setup(session, *, balance: int = 0, used: int = 50 * GB, rule: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    cust = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000)
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        plan_id=plan.id,
        customer=cust,
        panel=panel,
        plan=plan,
        panel_username=f"auto{cust.id}",
        status=ServiceStatus.ACTIVE,
        data_limit=50 * GB,
        used_bytes=used,
        expire_at=utcnow() + timedelta(days=10),
        auto_renew=rule if rule is not None else {"mode": "renew", "plan_id": plan.id},
    )
    session.add(service)
    await session.flush()
    if balance:
        await wallet.reward(session, shop.id, cust.id, balance, key=f"test-{cust.id}")
    return shop, cust, plan, service


async def _notices(session, service_id: int) -> int:  # type: ignore[no-untyped-def]
    found = await session.scalars(select(Job).where(Job.kind == "auto_renew_notice"))
    return sum(1 for job in found if job.payload.get("service_id") == service_id)


async def test_due_service_renews_from_the_wallet_once(session) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan, service = await _setup(session, balance=250_000)
    assert service.id in await auto_renew.due(session)

    assert await auto_renew.run_service(session, service.id) == "placed"
    order = await session.scalar(select(Order).where(Order.service_id == service.id))
    assert order is not None
    assert order.kind == OrderKind.RENEW and order.plan_id == plan.id
    assert order.source == auto_renew.SOURCE and order.status == OrderStatus.PAID
    assert await wallet_balance(session, shop.id, cust.id) == 150_000
    assert service.auto_renew["last_order_id"] == order.id

    # The next rounds must not charge again while that order is still being delivered.
    assert await auto_renew.run_service(session, service.id) == "cooldown"
    later = utcnow() + auto_renew.COOLDOWN + timedelta(minutes=1)
    assert await auto_renew.run_service(session, service.id, now=later) == "busy"
    count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.service_id == service.id)
    )
    assert count == 1
    assert await wallet_balance(session, shop.id, cust.id) == 150_000


async def test_short_wallet_keeps_the_rule_and_tells_the_customer_once(session) -> None:  # type: ignore[no-untyped-def]
    shop, cust, _, service = await _setup(session, balance=30_000)

    assert await auto_renew.run_service(session, service.id) == "short"
    assert service.auto_renew["short"] is True and not service.auto_renew.get("stopped")
    assert auto_renew.is_on(service)
    assert (
        await session.scalar(select(func.count()).select_from(Order).where(Order.service_id == service.id))
        == 0
    )
    assert await wallet_balance(session, shop.id, cust.id) == 30_000
    assert await _notices(session, service.id) == 1

    assert await auto_renew.run_service(session, service.id) == "short"
    assert await _notices(session, service.id) == 1  # said once, not every minute

    await wallet.reward(session, shop.id, cust.id, 100_000, key=f"more-{cust.id}")
    assert await auto_renew.run_service(session, service.id) == "placed"
    assert "short" not in service.auto_renew
    assert await wallet_balance(session, shop.id, cust.id) == 30_000


async def test_rule_stops_when_its_plan_is_gone(session, fake_bot) -> None:  # type: ignore[no-untyped-def]
    _, cust, plan, service = await _setup(session, balance=500_000)
    plan.is_active = False
    await session.flush()

    assert await auto_renew.run_service(session, service.id) == "stopped"
    assert service.auto_renew["stopped"] == "plan_unavailable"
    assert not auto_renew.is_on(service)
    assert service.id not in await auto_renew.due(session)
    assert await _notices(session, service.id) == 1

    job = SimpleNamespace(
        payload={"service_id": service.id, "reason": "plan_unavailable"}, kind="auto_renew_notice"
    )
    await handlers.auto_renew_notice(session, job)  # type: ignore[arg-type]
    sent = fake_bot.last(SendMessage)
    assert sent.chat_id == cust.telegram_id
    assert service.panel_username in sent.text


async def test_nothing_happens_when_not_due(session) -> None:  # type: ignore[no-untyped-def]
    _, _, _, service = await _setup(session, balance=500_000, used=GB)
    assert service.id not in await auto_renew.due(session)
    assert await auto_renew.run_service(session, service.id) == "idle"


async def test_enable_checks_extras_and_merges_parts(session) -> None:  # type: ignore[no-untyped-def]
    _, _, _, service = await _setup(session, used=GB, rule={})
    service.auto_renew = None
    await session.flush()

    rule = await auto_renew.enable(session, service, gb=10)
    assert rule == {"mode": "extra", "gb": 10}
    rule = await auto_renew.enable(session, service, days=30)
    assert rule == {"mode": "extra", "gb": 10, "days": 30}
    await auto_renew.drop_part(session, service, "gb")
    assert service.auto_renew == {"mode": "extra", "days": 30}
    await auto_renew.drop_part(session, service, "days")
    assert service.auto_renew is None

    with pytest.raises(orders.OrderError):
        await auto_renew.enable(session, service, plan_id=999_999)
    with pytest.raises(orders.OrderError):
        await auto_renew.enable(session, service, gb=100_000)
    assert service.auto_renew is None


async def test_due_pages_past_the_old_scan_window(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A small page reproduces the original 4*BATCH truncation without thousands of rows.
    monkeypatch.setattr(auto_renew, "BATCH", 2)
    _, _, _, template = await _setup(session, used=GB)
    services = []
    for i in range(9):
        row = Service(
            shop_id=template.shop_id,
            customer_id=template.customer_id,
            panel_id=template.panel_id,
            plan_id=template.plan_id,
            panel_username=f"scan-{template.id}-{i}",
            status=ServiceStatus.ACTIVE,
            data_limit=50 * GB,
            used_bytes=50 * GB if i == 8 else GB,
            expire_at=utcnow() + timedelta(days=10),
            auto_renew={"mode": "renew", "plan_id": template.plan_id},
        )
        session.add(row)
        services.append(row)
    await session.flush()
    assert services[-1].id in await auto_renew.due(session, after_id=template.id)
    assert (
        await auto_renew.due(
            session,
            after_id=template.id,
            through_id=services[-2].id,
        )
        == []
    )


async def test_due_cursor_reaches_later_due_services(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(auto_renew, "BATCH", 2)
    _, _, _, template = await _setup(session)
    rows = []
    for i in range(4):
        row = Service(
            shop_id=template.shop_id,
            customer_id=template.customer_id,
            panel_id=template.panel_id,
            plan_id=template.plan_id,
            panel_username=f"due-{template.id}-{i}",
            status=ServiceStatus.ACTIVE,
            data_limit=50 * GB,
            used_bytes=50 * GB,
            expire_at=utcnow() + timedelta(days=10),
            auto_renew={"mode": "renew", "plan_id": template.plan_id},
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    first = await auto_renew.due(session, after_id=template.id, through_id=rows[-1].id)
    second = await auto_renew.due(session, after_id=first[-1], through_id=rows[-1].id)
    assert first == [rows[0].id, rows[1].id]
    assert second == [rows[2].id, rows[3].id]
    assert await auto_renew.due(session, after_id=second[-1], through_id=rows[-1].id) == []
