"""Customer-owned Mini App profile/settings (F-07); identity is read-only."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.db.models import Customer


def masked_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in phone if c in "0123456789")
    return "••••" + digits[-2:] if len(digits) >= 6 else "••••"


def profile_view(customer: Customer) -> dict[str, Any]:
    return {
        "phone_masked": masked_phone(customer.phone),
        "member_since": customer.created_at.isoformat(),
    }


async def set_alerts(session: AsyncSession, customer: Customer, *, muted: bool) -> dict[str, bool]:
    # Only this field is refreshed/written, preserving concurrent marketing and locale changes.
    await session.refresh(customer, ["alerts_muted"], with_for_update=True)
    before = {"alerts_muted": customer.alerts_muted}
    if customer.alerts_muted != muted:
        customer.alerts_muted = muted
        await record(
            session,
            shop_id=customer.shop_id,
            actor=Actor("customer", customer.id),
            action="customer.alert_preferences",
            entity="customer",
            entity_id=customer.id,
            before=before,
            after={"alerts_muted": muted},
        )
    return {"alerts_muted": customer.alerts_muted}
