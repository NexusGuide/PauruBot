"""Transactional ticket media and replay helpers. No public URLs or client-selected file IDs."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Service, Ticket, TicketMessage, TicketStatus
from paurubot.security.ticket_files import MAX_BYTES as MAX_BYTES
from paurubot.security.ticket_files import validate as validate

MAX_TICKET_BYTES = 16 * 1024 * 1024
MAX_CUSTOMER_BYTES = 32 * 1024 * 1024
MAX_FILES = 16
RETENTION_DAYS = 30


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_replay(previous: str | None, current: str) -> None:
    if previous != current:
        raise HTTPException(status_code=409, detail="ticket_request_conflict")


async def replayed(
    session: AsyncSession, ticket: Ticket, customer_id: int, key: str | None, digest: str
) -> TicketMessage | None:
    if not key:
        return None
    previous = await session.scalar(
        select(TicketMessage).where(
            TicketMessage.ticket_id == ticket.id,
            TicketMessage.author_type == "customer",
            TicketMessage.author_id == customer_id,
            TicketMessage.request_key == key,
        )
    )
    if previous is not None:
        check_replay(previous.request_hash, digest)
    return previous


def service_context(service: Service) -> dict[str, Any]:
    # Never snapshot credentials, links, arbitrary panel data or private configuration.
    return {
        "id": service.id,
        "status": service.status.value,
        "used_bytes": service.used_bytes,
        "data_limit": service.data_limit,
        "expire_at": service.expire_at.isoformat() if service.expire_at else None,
    }


async def quota(session: AsyncSession, ticket: Ticket, size: int) -> None:
    # The customer lock serializes quota checks across that customer's tickets.
    count, total = (
        await session.execute(
            select(
                func.count(TicketMessage.id), func.coalesce(func.sum(TicketMessage.attachment_size), 0)
            ).where(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.attachment_expired.is_(False),
                TicketMessage.attachment_size.is_not(None),
            )
        )
    ).one()
    owned_total = await session.scalar(
        select(func.coalesce(func.sum(TicketMessage.attachment_size), 0))
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .where(
            Ticket.customer_id == ticket.customer_id,
            Ticket.shop_id == ticket.shop_id,
            TicketMessage.attachment_expired.is_(False),
        )
    )
    if (
        count >= MAX_FILES
        or int(total or 0) + size > MAX_TICKET_BYTES
        or int(owned_total or 0) + size > MAX_CUSTOMER_BYTES
    ):
        raise HTTPException(status_code=409, detail="ticket_file_quota")


async def prune(session: AsyncSession) -> int:
    """Remove payloads 30 days after a closed ticket's last activity, in bounded locked batches.

    Tombstones and request hashes survive for replay. Cascading ticket deletion also deletes
    blobs. There are no staging files or filesystem orphans, even on transaction rollback.
    """
    ids = list(
        await session.scalars(
            select(Ticket.id)
            .where(
                Ticket.status == TicketStatus.CLOSED,
                Ticket.updated_at < utcnow() - timedelta(days=RETENTION_DAYS),
                Ticket.id.in_(
                    select(TicketMessage.ticket_id).where(TicketMessage.attachment_data.is_not(None))
                ),
            )
            .order_by(Ticket.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    )
    if not ids:
        return 0
    result = await session.execute(
        update(TicketMessage)
        .where(
            TicketMessage.ticket_id.in_(ids),
            TicketMessage.attachment_data.is_not(None),
        )
        .values(attachment_data=None, attachment_expired=True)
    )
    return int(getattr(result, "rowcount", 0) or 0)
