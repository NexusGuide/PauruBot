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


PENDING_START_TTL = 7 * 86400


def _pending_key(shop_id: int, customer_id: int) -> str:
    return f"pending-start:{shop_id}:{customer_id}"


async def remember_pending_start(shop_id: int, customer_id: int, ref_telegram_id: int | None = None) -> None:
    """Remember a newcomer's /start referral and welcome gift while a gate holds them."""
    import json

    from paurubot import kv

    payload = json.dumps({"ref": ref_telegram_id})
    await kv.get_redis().set(_pending_key(shop_id, customer_id), payload, ex=PENDING_START_TTL)


async def take_pending_start(shop_id: int, customer_id: int) -> tuple[bool, int | None]:
    """Pop any saved onboarding state for a newcomer who just cleared the gate."""
    import json

    from paurubot import kv

    redis = kv.get_redis()
    key = _pending_key(shop_id, customer_id)
    raw = await redis.get(key)
    if raw is None:
        return False, None
    await redis.delete(key)
    try:
        data = json.loads(raw)
    except Exception:
        return True, None
    ref = data.get("ref") if isinstance(data, dict) else None
    return True, int(ref) if isinstance(ref, int) and ref > 0 else None


async def complete_onboarding(
    session: Any,
    shop_id: int,
    customer: Customer,
    cfg: dict[str, Any],
    *,
    ref_telegram_id: int | None = None,
) -> int:
    """Bind any referral, pay the one-time welcome gift, and queue the new-user report."""
    from paurubot.core import customers
    from paurubot.worker import queue

    if ref_telegram_id and cfg.get("referral_enabled"):
        referrer = await customers.set_referrer(session, customer, ref_telegram_id)
        if referrer is not None:
            await queue.enqueue(
                session,
                "notify",
                "referral_joined",
                {"customer_id": referrer.id, "name": customer.display_name},
                shop_id=shop_id,
                dedupe_key=f"ref-joined:{customer.id}",
            )
            if cfg.get("referral_config_counts") == "join":
                from paurubot.core import referral_rewards

                await referral_rewards.maybe_announce(session, referrer, cfg)
    welcome_gift = 0
    gift = int(cfg.get("start_gift_amount") or 0)
    if gift > 0 and (customer.referrer_id or not cfg.get("start_gift_referred_only")):
        from paurubot.ledger import wallet

        posted = await wallet.reward(
            session,
            shop_id,
            customer.id,
            gift,
            key=f"start_gift:{customer.id}",
            memo="welcome gift",
        )
        if posted.created:
            welcome_gift = gift
    await queue.enqueue(
        session,
        "notify",
        "new_user_report",
        {"customer_id": customer.id},
        shop_id=shop_id,
        dedupe_key=f"new-user:{customer.id}",
    )
    return welcome_gift
