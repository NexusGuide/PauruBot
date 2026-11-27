"""Bring users that already exist on a panel under the bot's management (TEN-10).

Each imported user becomes a service owned by a customer: either the customer whose Telegram ID
is written in the panel user's note or username, or one chosen customer (typically a reseller).
Nothing is changed on the panel itself.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import customers
from paurubot.db.models import Customer, Panel, Service, ServiceStatus
from paurubot.panels import PanelUser, driver_for
from paurubot.provisioning.lifecycle import apply_panel_user, new_sub_token

log = logging.getLogger(__name__)

PAGE = 200
MAX_USERS = 50_000
_NOTE_RE = re.compile(r"(?:tg|telegram|tgid|id)\s*[:=#_ -]?\s*(\d{5,15})", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\s*(\d{5,15})\s*$")
# Our own telegram_id usernames look like "<prefix><id>_<4 hex>"; others often use "<prefix>_<id>".
_USERNAME_RE = re.compile(r"^[a-z]{0,10}_?(\d{6,15})(?:_[0-9a-f]{4})?$")


def telegram_id_of(user: PanelUser) -> int | None:
    """A Telegram ID written in the panel user's note or encoded in its username, if any."""
    for pattern in (_DIGITS_RE, _NOTE_RE):
        match = pattern.search(user.note or "")
        if match:
            return int(match.group(1))
    match = _USERNAME_RE.match(user.username.lower())
    return int(match.group(1)) if match else None


async def all_users(panel: Panel) -> list[PanelUser]:
    driver = await driver_for(panel)
    users: list[PanelUser] = []
    seen: set[str] = set()
    offset = 0
    while offset < MAX_USERS:
        page, total = await driver.list_users(offset, PAGE)
        if not page:
            break
        added = 0
        for user in page:
            if user.username not in seen:
                seen.add(user.username)
                users.append(user)
                added += 1
        offset += len(page)
        if added == 0:
            break
        if total is not None:
            if offset >= total:
                break
        elif len(page) < PAGE:
            break
    return users


async def tracked_usernames(session: AsyncSession, panel: Panel) -> set[str]:
    return set(
        await session.scalars(
            select(Service.panel_username).where(
                Service.panel_id == panel.id, Service.status != ServiceStatus.DELETED
            )
        )
    )


async def untracked(session: AsyncSession, panel: Panel) -> list[PanelUser]:
    known = await tracked_usernames(session, panel)
    return [u for u in await all_users(panel) if u.username not in known]


@dataclass(slots=True)
class ImportResult:
    imported: int = 0
    skipped_existing: int = 0
    unassigned: int = 0
    customers_created: int = 0
    usernames: list[str] = field(default_factory=list)


async def run_import(
    session: AsyncSession,
    panel: Panel,
    *,
    usernames: list[str] | None,
    customer_id: int | None,
    match_telegram: bool,
) -> ImportResult:
    """Create services for the selected panel users. Commits in batches; safe to run twice."""
    result = ImportResult()
    fallback: Customer | None = None
    if customer_id is not None:
        fallback = await session.get(Customer, customer_id)
        if fallback is None or fallback.shop_id != panel.shop_id:
            raise ValueError("customer not in this shop")
    wanted = set(usernames) if usernames is not None else None
    known = await tracked_usernames(session, panel)
    pending = 0
    for user in await all_users(panel):
        if wanted is not None and user.username not in wanted:
            continue
        if user.username in known:
            result.skipped_existing += 1
            continue
        owner = None
        if match_telegram:
            telegram_id = telegram_id_of(user)
            if telegram_id is not None:
                owner, created = await customers.upsert_from_telegram(
                    session, panel.shop_id, telegram_id, username=None, first_name=None
                )
                result.customers_created += int(created)
        owner = owner or fallback
        if owner is None:
            result.unassigned += 1
            continue
        service = Service(
            shop_id=panel.shop_id,
            customer_id=owner.id,
            panel_id=panel.id,
            panel_username=user.username,
            panel_data=user.data or {},
            label=user.username,
            status=ServiceStatus.ACTIVE,
            sub_token=new_sub_token(),
        )
        apply_panel_user(service, user)
        session.add(service)
        known.add(user.username)
        result.imported += 1
        if len(result.usernames) < 50:
            result.usernames.append(user.username)
        pending += 1
        if pending >= PAGE:
            await session.commit()
            pending = 0
    await session.commit()
    return result
