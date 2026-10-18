"""Shop-wide inbox records. No derived alert, delivery job or financial side effect."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.db.base import utcnow
from paurubot.db.models import Customer
from paurubot.db.models.announcements import Announcement, AnnouncementRead


class InboxError(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_key: UUID
    title: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4096)
    kind: Literal["operational", "marketing"]
    starts_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @field_validator("title", "text")
    @classmethod
    def no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("NUL is not valid text")
        return value

    @model_validator(mode="after")
    def window(self) -> Publish:
        if self.starts_at and self.expires_at and self.expires_at <= self.starts_at:
            raise ValueError("expiry must follow start")
        return self


def _fingerprint(body: Publish) -> str:
    value = body.model_dump(mode="json", exclude={"request_key", "starts_at", "expires_at"})
    for key in ("starts_at", "expires_at"):
        date = getattr(body, key)
        value[key] = date.astimezone(UTC).isoformat() if date else None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def item(row: Announcement, state: AnnouncementRead | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "text": row.text,
        "kind": row.kind,
        "starts_at": row.starts_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "seen_at": state.seen_at.isoformat() if state else None,
        "read_at": state.read_at.isoformat() if state and state.read_at else None,
    }


async def publish(session: AsyncSession, shop_id: int, actor: Actor, body: Publish) -> Announcement:
    fingerprint = _fingerprint(body)
    # Replay is checked before time validity: an acknowledged publication remains replayable later.
    existing = await session.scalar(
        select(Announcement).where(
            Announcement.shop_id == shop_id,
            Announcement.request_key == str(body.request_key),
        )
    )
    if existing:
        if existing.fingerprint != fingerprint:
            raise InboxError("announcement_conflict")
        return existing
    now = utcnow()
    starts_at = body.starts_at or now
    if body.expires_at and body.expires_at <= starts_at:
        raise InboxError("announcement_window")
    new_id = await session.scalar(
        insert(Announcement)
        .values(
            shop_id=shop_id,
            created_by_staff_id=actor.id,
            request_key=str(body.request_key),
            fingerprint=fingerprint,
            title=body.title,
            text=body.text,
            kind=body.kind,
            starts_at=starts_at,
            expires_at=body.expires_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["shop_id", "request_key"])
        .returning(Announcement.id)
    )
    row = await session.scalar(
        select(Announcement).where(
            Announcement.shop_id == shop_id,
            Announcement.request_key == str(body.request_key),
        )
    )
    assert row is not None
    if row.fingerprint != fingerprint:
        raise InboxError("announcement_conflict")
    if new_id is not None:
        await record(
            session,
            shop_id=shop_id,
            actor=actor,
            action="announcement.publish",
            entity="announcement",
            entity_id=row.id,
            after={"kind": row.kind, "starts_at": row.starts_at.isoformat()},
        )
    return row


async def revoke(session: AsyncSession, shop_id: int, actor: Actor, row_id: int) -> Announcement:
    row = await session.scalar(
        select(Announcement)
        .where(Announcement.shop_id == shop_id, Announcement.id == row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise InboxError("announcement_not_found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await record(
            session,
            shop_id=shop_id,
            actor=actor,
            action="announcement.revoke",
            entity="announcement",
            entity_id=row.id,
        )
    return row


async def _customer(session: AsyncSession, shop_id: int, customer_id: int) -> Customer:
    row = await session.scalar(
        select(Customer)
        .where(
            Customer.id == customer_id,
            Customer.shop_id == shop_id,
            Customer.is_banned.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise InboxError("announcement_not_found")
    return row


def _visible(customer: Customer, now: datetime) -> Any:
    return and_(
        Announcement.shop_id == customer.shop_id,
        Announcement.revoked_at.is_(None),
        Announcement.starts_at <= now,
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        Announcement.kind != "marketing" if customer.marketing_opt_out else true(),
    )


async def inbox(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    *,
    before: int | None = None,
    limit: int = 20,
    unread: bool = False,
) -> dict[str, Any]:
    customer = await _customer(session, shop_id, customer_id)
    limit = max(1, min(limit, 50))
    query = (
        select(Announcement, AnnouncementRead)
        .outerjoin(
            AnnouncementRead,
            and_(
                AnnouncementRead.announcement_id == Announcement.id,
                AnnouncementRead.customer_id == customer_id,
            ),
        )
        .where(_visible(customer, utcnow()))
    )
    if before is not None:
        query = query.where(Announcement.id < before)
    if unread:
        query = query.where(AnnouncementRead.read_at.is_(None))
    rows = (await session.execute(query.order_by(Announcement.id.desc()).limit(limit + 1))).all()
    page = rows[:limit]
    return {
        "items": [item(row, state) for row, state in page],
        "next_before": page[-1][0].id if len(rows) > limit else None,
    }


async def mark(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    row_id: int,
    action: Literal["seen", "read"],
) -> dict[str, Any]:
    customer = await _customer(session, shop_id, customer_id)
    # Serialize with revocation. No body on an unavailable/cross-shop identifier, even a known one.
    row = await session.scalar(
        select(Announcement)
        .where(Announcement.id == row_id, _visible(customer, utcnow()))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise InboxError("announcement_not_found")
    now = utcnow()
    statement = insert(AnnouncementRead).values(
        announcement_id=row.id,
        customer_id=customer.id,
        seen_at=now,
        read_at=now if action == "read" else None,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["announcement_id", "customer_id"],
            set_={"read_at": func.coalesce(AnnouncementRead.read_at, statement.excluded.read_at)},
        )
    )
    state = await session.scalar(
        select(AnnouncementRead)
        .where(
            AnnouncementRead.announcement_id == row.id,
            AnnouncementRead.customer_id == customer_id,
        )
        .execution_options(populate_existing=True)
    )
    return item(row, state)
