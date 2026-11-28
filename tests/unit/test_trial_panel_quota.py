"""F-20: a panel can cap its own trials per 24 hours; the next panel then takes over.

Before, the only cap was the shop's daily quota. A shop with one small panel for trials and
bigger ones for sales could not keep trials from filling the small one.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from paurubot.core import resale, trials
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Panel, TrialGrant
from tests import factories
from tests.unit.test_reseller_bot import TOKEN
from tests.unit.test_reseller_bot import env as env  # noqa: F401  (fixture)


async def _shop(session, quotas: list[int | None], **cfg):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panels = []
    for quota in quotas:
        panel = await factories.panel(session, shop)
        if quota is not None:
            panel.options = {**(panel.options or {}), trials.PANEL_QUOTA: quota}
        panels.append(panel)
    await shop_settings.set_many(session, shop.id, cfg)
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()
    return shop, panels


async def _trial(session, shop):  # type: ignore[no-untyped-def]
    customer = await factories.customer(session, shop)
    order = await trials.request(session, customer)
    await session.commit()
    return order


def test_only_a_positive_whole_number_is_a_quota() -> None:
    for value, expected in ((5, 5), (0, 0), (-1, 0), ("5", 0), (True, 0), (2.5, 0), (None, 0)):
        assert trials.panel_quota(Panel(options={trials.PANEL_QUOTA: value})) == expected
    assert trials.panel_quota(Panel(options=None)) == 0


async def test_a_full_panel_hands_trials_to_the_next(session) -> None:  # type: ignore[no-untyped-def]
    shop, (small, big) = await _shop(session, [1, None])
    first = await _trial(session, shop)
    second = await _trial(session, shop)
    third = await _trial(session, shop)
    assert [o.params["panel_id"] for o in (first, second, third)] == [small.id, big.id, big.id]
    grants = list(await session.scalars(select(TrialGrant.panel_id).where(TrialGrant.shop_id == shop.id)))
    assert sorted(grants) == sorted([small.id, big.id, big.id])


async def test_a_chosen_trial_panel_that_is_full_says_so(session) -> None:  # type: ignore[no-untyped-def]
    shop, (small, _other) = await _shop(session, [2, None])
    await shop_settings.set_many(session, shop.id, {"trial_panel_id": small.id})
    await session.commit()
    await _trial(session, shop)
    await _trial(session, shop)
    with pytest.raises(trials.TrialError, match="trial_quota_reached"):
        await _trial(session, shop)  # the chosen panel only, never another


async def test_the_quota_rolls_over_24_hours_and_failed_trials_do_not_count(session) -> None:  # type: ignore[no-untyped-def]
    shop, (only,) = await _shop(session, [1])
    order = await _trial(session, shop)
    with pytest.raises(trials.TrialError, match="trial_quota_reached"):
        await _trial(session, shop)
    # A trial whose provisioning failed gives its place back.
    await trials.release(session, order.params["grant_id"])
    await session.commit()
    order = await _trial(session, shop)
    assert order.params["panel_id"] == only.id
    # A day later the panel has room again.
    await session.execute(
        update(TrialGrant)
        .where(TrialGrant.id == order.params["grant_id"])
        .values(created_at=utcnow() - timedelta(days=1, minutes=1))
    )
    await session.commit()
    assert (await _trial(session, shop)).params["panel_id"] == only.id


async def test_a_panel_that_takes_no_trials_is_not_a_fallback(session) -> None:  # type: ignore[no-untyped-def]
    shop, (small, closed) = await _shop(session, [1, None])
    closed.options = {**(closed.options or {}), "allow_trials": False}
    await session.commit()
    await _trial(session, shop)
    with pytest.raises(trials.TrialError, match="trial_quota_reached"):
        await _trial(session, shop)


async def test_reseller_trials_count_against_the_parent_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    """The parent's panels serve its resellers' trials too, so one quota covers them all."""
    shop = env["shop"]
    for panel in await session.scalars(select(Panel).where(Panel.shop_id == shop.id)):
        panel.options = {**(panel.options or {}), trials.PANEL_QUOTA: 1}
    await shop_settings.set_many(session, shop.id, {"resale_trial_fee": 0})
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await resale.allow_feature(session, env["profile"], "trial", True)
    await session.commit()
    await resale.update(session, child, {"trial_enabled": True})
    await session.commit()
    shop_settings.invalidate(child.id)
    shop_settings.invalidate(shop.id)
    panels = int(await session.scalar(select(func.count(Panel.id)).where(Panel.shop_id == shop.id)) or 0)
    for _ in range(panels):  # the parent's own customers fill every panel
        await _trial(session, shop)
    with pytest.raises(trials.TrialError, match="trial_quota_reached"):
        await trials.request(session, await factories.customer(session, child, telegram_id=63099))


def test_the_admin_api_takes_only_a_whole_number_quota() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.panels import _clean_options

    assert _clean_options({trials.PANEL_QUOTA: 20}) == {trials.PANEL_QUOTA: 20}
    assert _clean_options({trials.PANEL_QUOTA: 0}) == {trials.PANEL_QUOTA: 0}
    assert _clean_options({trials.PANEL_QUOTA: None}) == {}
    for bad in (-1, 1_000_001, "5", 2.5, True):
        with pytest.raises(HTTPException, match="trial_daily_quota_invalid"):
            _clean_options({trials.PANEL_QUOTA: bad})


def test_the_admin_api_accepts_per_panel_trial_limits() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.panels import _clean_options

    limits = {"trial_days": 4, "trial_hours": 12, "trial_gb": 3, "trial_mb": 700}
    assert _clean_options(limits) == limits
    assert _clean_options({"trial_days": 0, "trial_mb": 0}) == {"trial_days": 0, "trial_mb": 0}
    for key, bad in (
        ("trial_days", 3_651),
        ("trial_hours", 721),
        ("trial_gb", 102_401),
        ("trial_mb", -1),
        ("trial_mb", "500"),
    ):
        with pytest.raises(HTTPException, match=f"{key}_invalid"):
            _clean_options({key: bad})
