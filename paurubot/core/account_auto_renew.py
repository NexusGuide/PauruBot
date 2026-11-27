"""Auto-renewal of panel admin accounts from the wallet (TEN-14, like F-02 for services).

The owner turns it on with one renewal choice: the plan's own renewal, or one of its packages
(``{"package": i}``). When the period is about to end or the traffic is nearly used up, the worker
places that renewal through the normal order path (draft, create, settle from the wallet), so the
plan's price and renewal rule apply exactly as for a renewal bought by hand. A short wallet keeps
the rule on: the owner is told once and it renews when the money is there. Anything else (the plan
gone or no longer offering that choice) stops it and tells the owner.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders, panel_accounts
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Panel, PanelAccount
from paurubot.worker import queue

log = logging.getLogger(__name__)

SOURCE = "auto"
LEAD = timedelta(hours=1)  # renew this long before the period ends
COOLDOWN = timedelta(minutes=30)
LOW_FLOOR = 256 * 1024**2
LOW_SHARE = 0.02
BUSY = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.PROVISIONING)
SHORT = "insufficient_balance"


class AutoRenewError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def choices(account: PanelAccount) -> list[dict[str, int]]:
    """What auto-renewal may repeat: the plan's own renewal and its packages (a custom size is
    chosen by hand each time)."""
    plan = account.plan
    if plan is None or not plan.renewable:  # an archived plan only stops new sales
        return []
    out: list[dict[str, int]] = [{}] if panel_accounts.plain_renewal(plan) else []
    out += [{"package": i} for i in range(len(panel_accounts.renew_packages(plan)))]
    return out


def is_on(account: PanelAccount) -> bool:
    found = account.auto_renew
    return isinstance(found, dict) and not found.get("stopped")


async def enable(session: AsyncSession, account: PanelAccount, choice: dict[str, Any]) -> None:
    clean = {"package": int(choice["package"])} if "package" in choice else {}
    if clean not in choices(account):
        raise AutoRenewError("renew_option_unavailable")
    account.auto_renew = clean
    await session.flush()


async def disable(session: AsyncSession, account: PanelAccount) -> None:
    account.auto_renew = None
    await session.flush()


def wants_renewal(account: PanelAccount, now: datetime) -> bool:
    if account.status not in {"active", "expired"}:
        return False  # a paused account is the owner's choice
    if account.expire_at is not None and account.expire_at - now <= LEAD:
        return True
    limit = int(account.data_limit or 0)
    if limit:
        left = limit - int(account.used_bytes or 0)
        return left <= max(LOW_FLOOR, int(limit * LOW_SHARE))
    return False


async def due(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        select(PanelAccount.id).where(
            PanelAccount.auto_renew.is_not(None), PanelAccount.status.in_(("active", "expired"))
        )
    )
    return list(rows)


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


async def run_account(session: AsyncSession, account_id: int, now: datetime | None = None) -> str:
    """Place the renewal the rule asks for, when it is due. Returns what happened."""
    now = now or utcnow()
    account = await session.scalar(
        select(PanelAccount)
        .where(PanelAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None or not is_on(account):
        return "off"
    if not wants_renewal(account, now):
        return "idle"
    found = dict(account.auto_renew or {})
    last_at = _when(found.get("last_at"))
    if last_at is not None and now - last_at < COOLDOWN:
        return "cooldown"
    last_id = int(found.get("last_order_id") or 0)
    if last_id:
        last = await session.get(Order, last_id)
        if last is not None and last.status in BUSY:
            return "busy"
        if last is not None and last.status in {OrderStatus.FAILED, OrderStatus.REFUNDED}:
            return await _stop(session, account, "order_failed")
    choice = {k: found[k] for k in ("package",) if k in found}
    if choice not in choices(account):
        return await _stop(session, account, "renew_option_unavailable")
    customer = await session.get(Customer, account.customer_id)
    if customer is None:
        return "off"
    if customer.is_banned:
        return "banned"
    panel = await session.get(Panel, account.panel_id)
    if panel is None or not panel.is_active:
        return "panel_inactive"
    if panel.national_mode:
        return "national_mode"
    try:
        async with session.begin_nested():
            draft = await panel_accounts.draft(
                session, customer, OrderKind.ACCOUNT_RENEW, account=account, package=choice.get("package")
            )
            order = await orders.create(session, customer, draft, source=SOURCE)
            await orders.settle_from_wallet(session, order.id)
    except orders.OrderError as exc:
        await session.refresh(account)
        if exc.key == SHORT:
            return await _short(session, account, exc.params)
        return await _stop(session, account, exc.key)
    current = dict(account.auto_renew or {})
    current.update(last_order_id=order.id, last_at=now.isoformat())
    current.pop("short", None)
    account.auto_renew = current
    await session.flush()
    log.info("auto-renew: account %s got renewal order %s", account.id, order.id)
    return "placed"


async def _short(session: AsyncSession, account: PanelAccount, params: dict[str, Any]) -> str:
    current = dict(account.auto_renew or {})
    if current.get("short"):
        return "short"
    current["short"] = True
    account.auto_renew = current
    await queue.enqueue(
        session,
        "notify",
        "account_auto_renew",
        {
            "account_id": account.id,
            "reason": SHORT,
            "balance": int(params.get("balance") or 0),
            "required": int(params.get("required") or 0),
        },
        shop_id=account.shop_id,
        dedupe_key=f"account_auto:{account.id}:short:{utcnow().isoformat()}",
    )
    await session.flush()
    return "short"


async def _stop(session: AsyncSession, account: PanelAccount, reason: str) -> str:
    current = dict(account.auto_renew or {})
    if current.get("stopped"):
        return "stopped"
    current.update(stopped=reason[:48], stopped_at=utcnow().isoformat())
    current.pop("short", None)
    account.auto_renew = current
    await queue.enqueue(
        session,
        "notify",
        "account_auto_renew",
        {"account_id": account.id, "reason": reason[:48]},
        shop_id=account.shop_id,
        dedupe_key=f"account_auto:{account.id}:stop:{current['stopped_at']}",
    )
    await session.flush()
    return "stopped"
