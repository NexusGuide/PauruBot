"""«Invite required» (settings › growth): a newcomer uses the shop only after naming an inviter.

The inviter's code is their Telegram id, the same one the invite link carries (`/start ref_<id>`),
so a newcomer can either open a friend's link or send the code. Customers who already bought, and
staff, are never asked: the rule is for new sign-ups, not for the shop's existing buyers.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from paurubot.db.models import Customer, Order, OrderStatus

_CODE = re.compile(r"^(?:/start\s+)?(?:ref_)?(\d{4,20})$", re.IGNORECASE)


def code_of(text: str) -> int | None:
    """The inviter's Telegram id in a message: `1234567`, `ref_1234567` or `/start ref_1234567`."""
    match = _CODE.match(text.strip())
    if match is None or (text.strip().startswith("/start") and "ref_" not in text.lower()):
        return None
    return int(match.group(1))


async def needs_inviter(session: Any, customer: Customer) -> bool:
    if customer.referrer_id is not None:
        return False
    bought = await session.scalar(
        select(Order.id)
        .where(Order.customer_id == customer.id, Order.status == OrderStatus.COMPLETED)
        .limit(1)
    )
    return bought is None


async def join_by_code(session: Any, customer: Customer, text: str, cfg: dict[str, Any]) -> bool:
    """Link the customer to the inviter named in `text`; True once they have one."""
    from paurubot.core import customers
    from paurubot.worker import queue

    code = code_of(text)
    if code is None:
        return False
    referrer = await customers.set_referrer(session, customer, code)
    if referrer is None:
        return False
    await queue.enqueue(
        session,
        "notify",
        "referral_joined",
        {"customer_id": referrer.id, "name": customer.display_name},
        shop_id=customer.shop_id,
    )
    if cfg.get("referral_config_counts") == "join":
        from paurubot.core import referral_rewards

        await referral_rewards.maybe_announce(session, referrer, cfg)
    return True
