"""REF-04: one phone number is one free trial, and brand-new accounts can be made to wait.

Before, the rule was one trial per account and phone numbers were stored as Telegram spelled
them, so a second Telegram account with the same number, or the same number written another
way, took another trial.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from paurubot.core import phones, trials
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Job, JobStatus
from tests import factories


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("989121234567", "989121234567"),
        ("+989121234567", "989121234567"),
        ("00989121234567", "989121234567"),
        ("09121234567", "989121234567"),
        ("۰۹۱۲۱۲۳۴۵۶۷", "989121234567"),
        ("+1 (415) 555-0100", "14155550100"),
        ("", None),
        ("12", None),
        ("1" * 16, None),
    ],
)
def test_one_spelling_per_number(raw: str, expected: str | None) -> None:
    assert phones.normalize(raw) == expected


async def _shop(session, **cfg):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await factories.panel(session, shop)
    await shop_settings.set_many(session, shop.id, cfg)
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()
    return shop


async def test_a_second_account_with_the_same_number_gets_no_trial(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    first, second, other = [await factories.customer(session, shop) for _ in range(3)]
    first.phone = phones.normalize("+989121234567")
    second.phone = phones.normalize("09121234567")  # same number, written the local way
    other.phone = phones.normalize("989350000000")
    await session.commit()
    shop_id, second_id, other_id = shop.id, second.id, other.id
    await trials.request(session, first)
    await session.commit()
    assert await trials.eligibility(session, second) == "trial_phone_used"
    with pytest.raises(trials.TrialError, match="trial_phone_used"):
        await trials.request(session, second)
    await session.rollback()
    assert await trials.eligibility(session, await session.get(Customer, other_id)) is None
    # A new trial generation lets every number try again.
    await shop_settings.set_many(session, shop_id, {"trial_generation": 2})
    await session.commit()
    assert await trials.eligibility(session, await session.get(Customer, second_id)) is None


async def test_brand_new_accounts_wait_when_the_shop_asks(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session, trial_min_account_minutes=30)
    fresh = await factories.customer(session, shop)
    await session.commit()
    assert await trials.eligibility(session, fresh) == "trial_too_new"
    fresh.created_at = utcnow() - timedelta(minutes=31)
    await session.commit()
    assert await trials.eligibility(session, fresh) is None
    await shop_settings.set_many(session, shop.id, {"trial_min_account_minutes": 0})
    newer = await factories.customer(session, shop)
    await session.commit()
    assert await trials.eligibility(session, newer) is None
