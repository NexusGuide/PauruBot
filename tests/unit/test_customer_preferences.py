"""F-07: read-only masked identity and independent, tenant-owned reminders."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from paurubot.core import customer_preferences
from paurubot.db.models import AuditLog, Service, ServiceStatus
from paurubot.notifications import handlers
from tests import factories


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        (None, None),
        ("", None),
        ("123", "••••"),
        ("+98 912 345 6789", "••••89"),
        ("not-a-phone", "••••"),
        ("۱۲۳۴۵۶۷۸۹", "••••"),
    ],
)
def test_phone_mask_never_exposes_a_full_number(phone: str | None, expected: str | None) -> None:
    assert customer_preferences.masked_phone(phone) == expected


async def test_alert_choice_is_idempotent_audited_and_independent(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    customer.marketing_opt_out = True
    customer.locale = "en"
    customer.phone = "+989123456789"
    await session.flush()
    first = await customer_preferences.set_alerts(session, customer, muted=True)
    await session.commit()
    assert await customer_preferences.set_alerts(session, customer, muted=True) == first
    assert first == {"alerts_muted": True}
    assert customer.marketing_opt_out is True and customer.locale == "en"
    assert customer.phone == "+989123456789"
    view = customer_preferences.profile_view(customer)
    assert set(view) == {"phone_masked", "member_since"}
    assert view["phone_masked"] == "••••89"
    assert view["member_since"] == customer.created_at.isoformat()
    conditions = (AuditLog.entity_id == customer.id, AuditLog.action == "customer.alert_preferences")
    assert await session.scalar(select(func.count(AuditLog.id)).where(*conditions)) == 1
    entry = await session.scalar(select(AuditLog).where(*conditions))
    assert entry.actor_type == "customer" and entry.actor_id == customer.id and entry.shop_id == shop.id
    assert await customer_preferences.set_alerts(session, customer, muted=False) == {"alerts_muted": False}


async def test_same_telegram_user_has_separate_shop_reminders(session) -> None:  # type: ignore[no-untyped-def]
    first_shop = await factories.shop(session)
    second_shop = await factories.shop(session)
    first = await factories.customer(session, first_shop, telegram_id=917001)
    second = await factories.customer(session, second_shop, telegram_id=917001)
    await customer_preferences.set_alerts(session, first, muted=True)
    assert first.alerts_muted is True and second.alerts_muted is False


@pytest.mark.parametrize(
    "alert, delivered",
    [("low_volume", False), ("expiring", False), ("expired", True), ("limited", True), ("deleted", True)],
)
async def test_muting_reminders_does_not_suppress_service_endings(
    session, monkeypatch, alert: str, delivered: bool
) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    customer.alerts_muted = True
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="profile-reminders",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.flush()
    send = AsyncMock()
    monkeypatch.setattr(handlers, "_texts", AsyncMock(return_value=lambda key, **kw: key))
    monkeypatch.setattr(handlers, "_to_customer", send)
    monkeypatch.setattr(handlers, "_renew_markup", lambda *args: None)
    await handlers.service_alert(session, SimpleNamespace(payload={"service_id": service.id, "alert": alert}))
    assert send.await_count == int(delivered)
