"""SEC-F15: an exhausted service ages from the moment its data ran out.

Cleanup used to age an exhausted (LIMITED) service by its end date, or by its last row update when
it had none. A service with time left was kept long after its data ran out. A service without an
end date could be deleted days after exhaustion, or never: any row change resets updated_at, and
an old updated_at says nothing about when the data ran out.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from paurubot.core import settings as shop_settings
from paurubot.db.models import PanelKind, Service, ServiceStatus
from paurubot.panels import GB, registry
from paurubot.panels.base import PanelUser
from paurubot.provisioning import lifecycle, sync
from tests import factories
from tests.fakes.panels import FakeTokenPanel

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _drivers():  # type: ignore[no-untyped-def]
    registry._cache.clear()
    yield
    registry.test_transports.clear()
    registry._cache.clear()


def test_limited_at_follows_the_status() -> None:
    service = Service(status=ServiceStatus.LIMITED, panel_username="x")
    first = service.limited_at
    assert first is not None
    service.status = ServiceStatus.LIMITED  # every sync assigns it again: the moment stays
    assert service.limited_at == first
    service.status = ServiceStatus.ACTIVE  # renewed or topped up
    assert service.limited_at is None
    service.status = ServiceStatus.LIMITED
    assert service.limited_at is not None and service.limited_at >= first


def test_panel_sync_marks_the_moment_data_runs_out() -> None:
    service = Service(status=ServiceStatus.ACTIVE, panel_username="x", data_limit=10 * GB)
    lifecycle.apply_panel_user(service, PanelUser("x", "active", 10 * GB, 10 * GB, None))
    assert service.status == ServiceStatus.LIMITED and service.limited_at is not None


async def _world(session, *, expire_at, limited_days_ago: float, updated_days_ago: float = 0):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = httpx.MockTransport(fake.handler)
    cust = await factories.customer(session, shop)
    name = f"lim_{secrets.token_hex(3)}"
    fake.users[name] = {
        "id": 1,
        "username": name,
        "status": "limited",
        "used_traffic": 10 * GB,
        "data_limit": 10 * GB,
        "expire": int(expire_at.timestamp()) if expire_at else 0,
        "sub": "s",
        "request": {},
    }
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        panel_username=name,
        status=ServiceStatus.LIMITED,
        data_limit=10 * GB,
        used_bytes=10 * GB,
        expire_at=expire_at,
        sub_token=secrets.token_urlsafe(12),
    )
    session.add(service)
    await session.flush()
    await session.execute(
        update(Service)
        .where(Service.id == service.id)
        .values(
            limited_at=NOW - timedelta(days=limited_days_ago),
            updated_at=NOW - timedelta(days=updated_days_ago),
        )
    )
    await shop_settings.set_many(session, shop.id, {"delete_expired_after_days": 7, "delete_warn_days": 0})
    await session.commit()
    return shop, service, fake


async def test_exhausted_service_with_time_left_is_cleaned_up_after_the_setting(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, expire_at=NOW + timedelta(days=20), limited_days_ago=10)
    assert await sync.cleanup_expired(session, shop.id) == 1
    await session.refresh(service)
    assert service.status == ServiceStatus.DELETED and fake.users == {}


async def test_recently_exhausted_service_is_kept_even_if_the_row_is_old(session) -> None:  # type: ignore[no-untyped-def]
    # No end date; the data ran out two days ago, the row was last updated forty days ago.
    shop, service, fake = await _world(session, expire_at=None, limited_days_ago=2, updated_days_ago=40)
    assert await sync.cleanup_expired(session, shop.id) == 0
    await session.refresh(service)
    assert service.status == ServiceStatus.LIMITED and len(fake.users) == 1


async def test_exhausted_service_without_end_date_is_cleaned_up_in_time(session) -> None:  # type: ignore[no-untyped-def]
    shop, service, fake = await _world(session, expire_at=None, limited_days_ago=9, updated_days_ago=0)
    assert await sync.cleanup_expired(session, shop.id) == 1


def test_mini_app_service_view_carries_when_data_ran_out() -> None:
    """SEC-F28: the Mini App keys its "data used up" alert by this moment."""
    from paurubot.api.routes.webapp import _service_base

    service = Service(id=5, status=ServiceStatus.LIMITED, panel_username="x", data_limit=1, used_bytes=1)
    view = _service_base(service)
    assert view["limited_at"] is not None
    service.status = ServiceStatus.ACTIVE
    assert _service_base(service)["limited_at"] is None
