"""A lapsed service is warned before the cleanup deletes it, and staff can keep one out (PGClockBot parity).

The cleanup used to delete a service that stayed expired or out of data longer than the shop
allows, with no word beforehand: the customer only heard once it was gone. Now the customer is
told `delete_warn_days` before the deletion, and the deletion waits at least that long after the
warning. Staff can keep a service out of the cleanup for a while, or for good.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Job, PanelKind, Service, ServiceStatus
from paurubot.i18n import Texts
from paurubot.panels import GB, registry
from paurubot.provisioning import sync
from tests import factories
from tests.fakes.panels import FakeTokenPanel

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _drivers():  # type: ignore[no-untyped-def]
    registry._cache.clear()
    yield
    registry.test_transports.clear()
    registry._cache.clear()


async def _world(session, *, ended_days_ago: float, warn_days: int = 2, trial: bool = False):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = httpx.MockTransport(fake.handler)
    cust = await factories.customer(session, shop)
    name = f"exp_{secrets.token_hex(3)}"
    ended = NOW - timedelta(days=ended_days_ago)
    fake.users[name] = {
        "id": 1,
        "username": name,
        "status": "expired",
        "used_traffic": GB,
        "data_limit": 10 * GB,
        "expire": int(ended.timestamp()),
        "sub": "s",
        "request": {},
    }
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        panel_username=name,
        status=ServiceStatus.EXPIRED,
        is_trial=trial,
        data_limit=10 * GB,
        used_bytes=GB,
        expire_at=ended,
        sub_token=secrets.token_urlsafe(12),
    )
    session.add(service)
    await shop_settings.set_many(
        session, shop.id, {"delete_expired_after_days": 7, "delete_warn_days": warn_days}
    )
    await session.commit()
    return shop, service, fake


async def _warnings(session, service: Service) -> list[Job]:  # type: ignore[no-untyped-def]
    jobs = await session.scalars(select(Job).where(Job.kind == "service_alert"))
    return [
        j
        for j in jobs
        if j.payload.get("service_id") == service.id and j.payload.get("alert") == "delete_soon"
    ]


async def _age_warning(session, service: Service, days: float) -> None:  # type: ignore[no-untyped-def]
    await session.execute(
        update(Service)
        .where(Service.id == service.id)
        .values(cleanup_warned_at=datetime.now(UTC) - timedelta(days=days))
    )
    await session.commit()


async def test_a_lapsed_service_is_warned_before_it_is_deleted(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, ended_days_ago=40)

    assert await sync.cleanup_expired(session, shop.id) == 0, "the first pass only warns"
    await session.refresh(service)
    assert service.status == ServiceStatus.EXPIRED and len(fake.users) == 1
    assert service.cleanup_warned_at is not None
    [warning] = await _warnings(session, service)
    assert warning.payload["days"] == 2

    assert await sync.cleanup_expired(session, shop.id) == 0, "a fresh warning gives its full time"
    assert len(await _warnings(session, service)) == 1, "warned once"

    await _age_warning(session, service, 2.1)
    assert await sync.cleanup_expired(session, shop.id) == 1
    await session.refresh(service)
    assert service.status == ServiceStatus.DELETED and fake.users == {}


async def test_the_warning_comes_shortly_before_the_deletion_date(session) -> None:  # type: ignore[no-untyped-def]
    shop, early, _ = await _world(session, ended_days_ago=4)  # deleted in 3 days, warned in 1
    assert await sync.cleanup_expired(session, shop.id) == 0
    assert await _warnings(session, early) == []

    shop, due, _ = await _world(session, ended_days_ago=5.5)  # deleted in 1.5 days
    await sync.cleanup_expired(session, shop.id)
    [warning] = await _warnings(session, due)
    assert warning.payload["days"] == 2


async def test_no_warning_setting_deletes_as_before(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, ended_days_ago=40, warn_days=0)
    assert await sync.cleanup_expired(session, shop.id) == 1
    assert await _warnings(session, service) == [] and fake.users == {}


async def test_a_trial_is_removed_without_a_renewal_warning(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, ended_days_ago=40, trial=True)
    assert await sync.cleanup_expired(session, shop.id) == 1
    assert await _warnings(session, service) == [] and fake.users == {}


def test_coming_back_to_life_forgets_the_warning() -> None:
    service = Service(status=ServiceStatus.EXPIRED, panel_username="x")
    service.cleanup_warned_at = NOW
    service.status = ServiceStatus.LIMITED  # still lapsed: the warning stands
    assert service.cleanup_warned_at == NOW
    service.status = ServiceStatus.ACTIVE  # renewed: a later lapse is warned again
    assert service.cleanup_warned_at is None


async def test_a_kept_service_is_neither_warned_nor_deleted(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, ended_days_ago=40)
    await session.execute(update(Service).where(Service.id == service.id).values(cleanup_exempt=True))
    await session.commit()
    assert await sync.cleanup_expired(session, shop.id) == 0
    assert await _warnings(session, service) == [] and len(fake.users) == 1


async def test_a_service_kept_for_a_while_is_warned_before_that_time_ends(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, ended_days_ago=40)
    await session.execute(
        update(Service).where(Service.id == service.id).values(keep_until=NOW + timedelta(days=10))
    )
    await session.commit()
    assert await sync.cleanup_expired(session, shop.id) == 0
    assert await _warnings(session, service) == [], "ten days left: too early to warn"

    await session.execute(
        update(Service).where(Service.id == service.id).values(keep_until=NOW + timedelta(days=1))
    )
    await session.commit()
    assert await sync.cleanup_expired(session, shop.id) == 0
    [warning] = await _warnings(session, service)
    assert warning.payload["days"] == 2, "never sooner than the warning promised"

    await _age_warning(session, service, 2.1)
    await session.execute(
        update(Service).where(Service.id == service.id).values(keep_until=NOW - timedelta(hours=1))
    )
    await session.commit()
    assert await sync.cleanup_expired(session, shop.id) == 1 and fake.users == {}


async def test_many_kept_services_do_not_starve_the_cleanup(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, kept, _ = await _world(session, ended_days_ago=40, warn_days=0)
    await session.execute(update(Service).where(Service.id == kept.id).values(cleanup_exempt=True))
    stale = Service(
        shop_id=shop.id,
        customer_id=kept.customer_id,
        panel_id=kept.panel_id,
        panel_username="other_stale",
        status=ServiceStatus.EXPIRED,
        expire_at=NOW - timedelta(days=40),
    )
    session.add(stale)
    await session.commit()
    monkeypatch.setattr(sync, "CLEANUP_BATCH", 1)
    assert await sync.cleanup_expired(session, shop.id) == 1, "the kept one is never picked"
    await session.refresh(stale)
    assert stale.status == ServiceStatus.DELETED


@pytest.mark.parametrize("lang", ["fa", "en"])
def test_the_warning_text_names_the_service_and_the_days(lang: str) -> None:
    text = Texts(lang)("alert_delete_soon", name="<b>x</b>", days=2, remaining="")
    assert "x" in text and "2" in text


async def test_the_warning_reaches_a_customer_who_muted_reminders(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.notifications import handlers
    from paurubot.worker import queue

    shop, service, _ = await _world(session, ended_days_ago=40)
    customer = await session.get(Customer, service.customer_id)
    assert customer is not None
    customer.alerts_muted = True
    service.cleanup_warned_at = NOW
    await session.commit()
    sent: list[tuple[str, object]] = []

    async def capture(session, customer, text, reply_markup=None, **_):  # type: ignore[no-untyped-def]
        sent.append((text, reply_markup))

    monkeypatch.setattr(handlers, "_to_customer", capture)
    job = queue.Claimed(
        id=0,
        kind="service_alert",
        payload={"service_id": service.id, "alert": "delete_soon", "days": 2},
        attempts=1,
        max_attempts=8,
        shop_id=shop.id,
        token="t",
    )
    await handlers.service_alert(session, job)
    [(text, markup)] = sent
    assert "2" in text and markup is not None, "the warning carries the renew buttons"


async def test_a_warning_held_for_the_morning_is_dropped_once_renewed(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.notifications import handlers
    from paurubot.worker import queue

    shop, service, _ = await _world(session, ended_days_ago=40)
    service.status = ServiceStatus.ACTIVE  # renewed overnight, before the warning went out
    await session.commit()
    sent: list[str] = []

    async def capture(session, customer, text, **_):  # type: ignore[no-untyped-def]
        sent.append(text)

    monkeypatch.setattr(handlers, "_to_customer", capture)
    job = queue.Claimed(
        id=0,
        kind="service_alert",
        payload={"service_id": service.id, "alert": "delete_soon", "days": 2},
        attempts=1,
        max_attempts=8,
        shop_id=shop.id,
        token="t",
    )
    await handlers.service_alert(session, job)
    assert sent == []
