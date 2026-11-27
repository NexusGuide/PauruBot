"""Apply an extract from another bot to one shop (MIG-01). Safe to run twice: every write is keyed."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.auth import rbac
from paurubot.core import customers as customer_service
from paurubot.core import phones
from paurubot.db.models import Customer, LedgerJournal, Panel, ResellerProfile, Service, ServiceStatus
from paurubot.ledger import wallet
from paurubot.migration.sources import Extract
from paurubot.panels import PanelError, PanelUser
from paurubot.provisioning import importer
from paurubot.provisioning.lifecycle import apply_panel_user, new_sub_token

log = logging.getLogger(__name__)
BATCH = 200
TOKEN_RE = re.compile(r"^[a-f0-9]{24}$")


@dataclass(slots=True)
class Report:
    source: str
    dry_run: bool
    customers_new: int = 0
    customers_existing: int = 0
    balance_credited: int = 0
    resellers: int = 0
    services_imported: int = 0
    services_existing: int = 0
    services_not_on_panel: int = 0
    services_unmapped: int = 0
    panel_errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summary(extract: Extract) -> dict[str, Any]:
    by_panel: dict[str, int] = {}
    for service in extract.services:
        by_panel[service.panel_key] = by_panel.get(service.panel_key, 0) + 1
    return {
        "source": extract.source,
        "customers": len(extract.customers),
        "resellers": sum(1 for c in extract.customers.values() if c.reseller),
        "balance_total": sum(c.balance for c in extract.customers.values()),
        "services": len(extract.services),
        "skipped_rows": extract.skipped,
        "panels": [
            {"key": key, "label": extract.panels.get(key, key), "services": count}
            for key, count in sorted(by_panel.items(), key=lambda item: -item[1])
        ],
    }


async def apply(
    session: AsyncSession,
    shop_id: int,
    extract: Extract,
    panel_map: dict[str, int],
    *,
    dry_run: bool = False,
) -> Report:
    report = Report(extract.source, dry_run)
    owners: dict[int, Customer] = {}
    pending = 0
    for old in extract.customers.values():
        existing = await session.scalar(
            select(Customer).where(Customer.shop_id == shop_id, Customer.telegram_id == old.telegram_id)
        )
        if existing is not None:
            report.customers_existing += 1
        else:
            report.customers_new += 1
        if dry_run:
            if old.balance > 0:
                idem = f"migrate:{extract.source}:{shop_id}:{old.telegram_id}"
                already = await session.scalar(
                    select(LedgerJournal.id).where(LedgerJournal.idempotency_key == idem)
                )
                if already is None:
                    report.balance_credited += old.balance
            if old.reseller:
                has_profile = (
                    await session.scalar(
                        select(ResellerProfile.id).where(ResellerProfile.customer_id == existing.id)
                    )
                    if existing is not None
                    else None
                )
                if has_profile is None:
                    report.resellers += 1
            continue
        customer, _ = await customer_service.upsert_from_telegram(
            session, shop_id, old.telegram_id, username=old.username, first_name=old.first_name
        )
        owners[old.telegram_id] = customer
        if old.phone and not customer.phone:
            customer.phone = phones.normalize(old.phone)
        if old.banned and not customer.is_banned:
            customer.is_banned, customer.ban_reason = True, f"imported from {extract.source}"
        if old.balance > 0:
            posted = await wallet.adjust(
                session,
                shop_id,
                customer.id,
                old.balance,
                key=f"migrate:{extract.source}:{shop_id}:{old.telegram_id}",
                actor="migration",
                memo=f"balance imported from {extract.source}",
            )
            if posted.created:
                report.balance_credited += old.balance
        if old.reseller:
            profile = await session.scalar(
                select(ResellerProfile).where(ResellerProfile.customer_id == customer.id)
            )
            if profile is None:
                session.add(
                    ResellerProfile(
                        shop_id=shop_id,
                        customer_id=customer.id,
                        permissions=sorted(rbac.DEFAULT_FOR_RESELLER),
                    )
                )
                report.resellers += 1
        pending += 1
        if pending >= BATCH:
            await session.commit()
            pending = 0
    if not dry_run:
        await session.commit()

    panels: dict[int, dict[str, PanelUser]] = {}
    known: dict[int, set[str]] = {}
    for panel_id in set(panel_map.values()):
        panel = await session.get(Panel, panel_id)
        if panel is None or panel.shop_id != shop_id:
            continue
        try:
            panels[panel_id] = {u.username: u for u in await importer.all_users(panel)}
        except PanelError as exc:
            report.panel_errors.append(f"{panel.name}: {exc}"[:200])
            panels[panel_id] = {}
        known[panel_id] = await importer.tracked_usernames(session, panel)

    for old_service in extract.services:
        target = panel_map.get(old_service.panel_key)
        if target is None or target not in panels:
            report.services_unmapped += 1
            continue
        panel_id = target
        if old_service.username in known[panel_id]:
            report.services_existing += 1
            continue
        user = panels[panel_id].get(old_service.username)
        if user is None:
            report.services_not_on_panel += 1
            continue
        report.services_imported += 1
        known[panel_id].add(old_service.username)
        if dry_run:
            continue
        owner = owners.get(old_service.telegram_id)
        if owner is None:
            owner, _ = await customer_service.upsert_from_telegram(
                session, shop_id, old_service.telegram_id, username=None, first_name=None
            )
            owners[old_service.telegram_id] = owner
        service = Service(
            shop_id=shop_id,
            customer_id=owner.id,
            panel_id=panel_id,
            panel_username=user.username,
            panel_data=user.data or {},
            label=old_service.plan or user.username,
            status=ServiceStatus.ACTIVE,
            sub_token=new_sub_token(),
        )
        apply_panel_user(service, user)
        session.add(service)
        pending += 1
        if pending >= BATCH:
            await session.commit()
            pending = 0
    if not dry_run:
        await session.commit()
    return report


def imports_dir() -> Path:
    from paurubot.config import get_settings

    return Path(get_settings().backup_dir) / "imports"


def upload_path(token: str) -> Path:
    if not TOKEN_RE.match(token):
        raise ValueError("bad_upload")
    return imports_dir() / f"{token}.dump"


# An upload holds the old bot's customers in plain form: it goes after the real import, and
# anything left behind (an import never run) after a few days (audit SEC-03).
UPLOAD_TTL = 3 * 86_400
_UPLOAD_NAME = re.compile(r"^[a-f0-9]{24}\.dump$")


def discard_upload(token: str) -> None:
    upload_path(token).unlink(missing_ok=True)


def prune_uploads(max_age: int = UPLOAD_TTL) -> int:
    """Delete uploads older than `max_age` seconds; returns how many went."""
    import time

    root = imports_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for path in root.iterdir():
        if _UPLOAD_NAME.match(path.name) and path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


async def load(token: str, source: str | None) -> Extract:
    import asyncio

    from paurubot.migration import sources

    path = upload_path(token)
    if not await asyncio.to_thread(path.is_file):
        raise ValueError("upload_missing")
    name = source if source else await asyncio.to_thread(sources.guess, path)
    reader = sources.SOURCES.get(name)
    if reader is None:
        raise ValueError("unknown_source")
    return await asyncio.to_thread(reader, path)
