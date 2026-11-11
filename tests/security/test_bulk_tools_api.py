"""Bot-only staff tools now in the web panel too: a wallet gift for an audience, a new trial round
for everyone, plan prices up or down at once, reseller ranking, and taking every pin down."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.core import settings as shop_settings
from paurubot.db.models import AuditLog, Job, JobStatus, Plan
from tests import factories
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
async def quiet_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()


async def _jobs(session, kind: str) -> list[Job]:  # type: ignore[no-untyped-def]
    return list(await session.scalars(select(Job).where(Job.kind == kind, Job.status == JobStatus.QUEUED)))


async def test_wallet_gift_for_an_audience(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    size = (await owner.call("GET", "/api/admin/tools/gift?audience=all")).json()
    assert size["count"] >= 1
    assert (
        await owner.call("POST", "/api/admin/tools/gift", {"amount": 500, "audience": "all"})
    ).status_code == 422
    assert (
        await owner.call("POST", "/api/admin/tools/gift", {"amount": 5000, "audience": "nobody"})
    ).status_code == 422
    done = await owner.call("POST", "/api/admin/tools/gift", {"amount": 5000, "audience": "buyers"})
    assert done.status_code == 200, done.text
    [job] = await _jobs(session, "gift")
    assert (
        job.shop_id == env.shop.id and job.payload["amount"] == 5000 and job.payload["audience"] == "buyers"
    )
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "wallet.gift_all"))
    support = await login(http, "u_support")
    assert (
        await support.call("POST", "/api/admin/tools/gift", {"amount": 5000, "audience": "all"})
    ).status_code == 403


async def test_a_new_trial_round_for_everyone(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    before = (await shop_settings.load(session, env.shop.id, fresh=True))["trial_generation"]
    owner = await login(http, "u_owner")
    done = await owner.call("POST", "/api/admin/tools/trial-reset")
    assert done.status_code == 200 and done.json()["generation"] == before + 1
    finance = await login(http, "u_finance")
    assert (await finance.call("POST", "/api/admin/tools/trial-reset")).status_code == 403


async def test_plan_prices_up_or_down_at_once(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    plan = await factories.plan(session, env.shop, env.panel, price=100_000)
    other = await factories.plan(session, env.other_shop, env.other_panel, price=100_000)
    await session.commit()
    owner = await login(http, "u_owner")
    for bad in (0, 91, -91):
        assert (await owner.call("POST", "/api/admin/tools/plan-prices", {"percent": bad})).status_code == 422
    done = await owner.call("POST", "/api/admin/tools/plan-prices", {"percent": 10})
    assert done.status_code == 200 and done.json()["plans"] >= 1
    await session.refresh(plan)
    await session.refresh(other)
    assert plan.price == 110_000 and other.price == 100_000  # another shop's plans never move
    await owner.call("POST", "/api/admin/tools/plan-prices", {"percent": -50})
    await session.refresh(plan)
    assert plan.price == 55_000
    assert (await session.scalar(select(Plan.price).where(Plan.id == plan.id))) == 55_000


async def test_reseller_ranking_and_unpin_all(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    ranking = (await owner.call("GET", "/api/admin/tools/reseller-ranking")).json()["items"]
    assert [r["rank"] for r in ranking] == list(range(1, len(ranking) + 1))
    done = await owner.call("POST", "/api/admin/tools/unpin-all")
    assert done.status_code == 200
    [job] = await _jobs(session, "unpin")
    assert job.shop_id == env.shop.id and job.payload.get("all") is True
    reseller = await login(http, "u_reseller")
    assert (await reseller.call("GET", "/api/admin/tools/reseller-ranking")).status_code == 403
    assert (await reseller.call("POST", "/api/admin/tools/unpin-all")).status_code == 403
