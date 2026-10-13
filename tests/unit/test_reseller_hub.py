from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import resellers
from paurubot.db.models import Job, JobStatus
from paurubot.ledger import wallet
from tests.unit.test_resellers import _reseller, env, isolate_queue  # noqa: F401


def test_runway_from_average_daily_spend() -> None:
    assert resellers.runway_from(50_000, 0, 0).days is None  # nothing spent: no estimate
    assert not resellers.runway_from(50_000, 0, 0).low
    runway = resellers.runway_from(30_000, 0, 70_000)
    assert runway.daily_spend == 10_000 and runway.days == 3.0 and not runway.low
    assert resellers.runway_from(30_000, 15_000, 70_000).days == 1.5  # debt comes off the balance
    short = resellers.runway_from(5_000, 0, 70_000)
    assert short.low and short.hours == 12
    assert resellers.runway_from(1_000, 5_000, 70_000).days == 0.0


class _T:
    def __call__(self, key: str, **values: object) -> str:
        return f"{key}:{values}"


def test_runway_line_switches_to_hours_under_a_day() -> None:
    assert resellers.runway_line(_T(), 3.0) == "reseller_runway_days:{'days': '3'}"
    assert resellers.runway_line(_T(), 1.5) == "reseller_runway_days:{'days': '1.5'}"
    assert resellers.runway_line(_T(), 0.5) == "reseller_runway_hours:{'hours': 12}"


async def test_metered_reseller_runway_warning_and_timeline(
    session: AsyncSession,
    env: dict[str, Any],  # noqa: F811
) -> None:
    shop = env["shop"]
    cust, profile = await _reseller(session, shop, balance=10_000, billing_mode="usage", gb_price=1_000)
    await wallet.usage_charge(session, shop.id, cust.id, 7_000, key=f"hub:{profile.id}:1")
    await session.commit()
    runway = await resellers.estimate_runway(session, profile)
    assert runway.balance == 3_000 and runway.daily_spend == 1_000 and runway.days == 3.0
    assert not await resellers.warn_low_balance(session, profile)  # three days left is fine

    await wallet.usage_charge(session, shop.id, cust.id, 2_000, key=f"hub:{profile.id}:2")
    await session.commit()
    runway = await resellers.estimate_runway(session, profile)
    assert runway.low and runway.days is not None and runway.days < 1
    # No threshold was set, but a metered reseller about to run dry is still warned, once.
    assert await resellers.warn_low_balance(session, profile)
    assert not await resellers.warn_low_balance(session, profile)
    jobs = list(
        await session.scalars(
            select(Job).where(Job.kind == "reseller_notice", Job.status == JobStatus.QUEUED)
        )
    )
    assert len(jobs) == 1 and jobs[0].payload["days"] == runway.days

    await resellers._audit_auto(session, profile, "reseller.suspend_auto")
    await session.commit()
    items = await resellers.timeline(session, profile)
    assert sorted(item["kind"] for item in items if item["type"] == "ledger") == ["topup", "usage", "usage"]
    audits = [item for item in items if item["type"] == "audit"]
    assert [item["kind"] for item in audits] == ["reseller.suspend_auto"]
    assert audits[0]["actor_type"] == "system"
    assert [item["at"] for item in items] == sorted((item["at"] for item in items), reverse=True)
