from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from paurubot.db.base import Base, TimestampMixin, UpdatedMixin, str_enum, utcnow
from paurubot.db.models.enums import JobStatus, TicketStatus


class Job(Base, UpdatedMixin):
    """Durable work queue (provisioning, deliveries, notifications) with leases and backoff."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_pick", "queue", "status", "run_at"),
        UniqueConstraint("dedupe_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    queue: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    run_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column()
    claim_token: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(128))
    finished_at: Mapped[datetime | None] = mapped_column()


class Broadcast(Base, UpdatedMixin):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    created_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    source_chat_id: Mapped[int | None] = mapped_column()
    source_message_id: Mapped[int | None] = mapped_column()
    text: Mapped[str | None] = mapped_column(Text)
    buttons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    audience: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[int] = mapped_column(default=0)
    finished_at: Mapped[datetime | None] = mapped_column()
    # F-16: why messages did not arrive (both also count in `failed`), when to start, and which run
    # of the job may send (bumped on every resume, so a stopped job never sends again).
    blocked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    deleted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scheduled_at: Mapped[datetime | None] = mapped_column()
    run: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Ticket(Base, UpdatedMixin):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_shop_status", "shop_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(128))
    status: Mapped[TicketStatus] = mapped_column(str_enum(TicketStatus), default=TicketStatus.OPEN)
    assigned_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    # "bot" for customer tickets, "panel" for a reseller writing to the owner from the web panel (SUP-02).
    source: Mapped[str] = mapped_column(String(16), default="bot", server_default="bot")


class TicketMessage(Base, TimestampMixin):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    author_type: Mapped[str] = mapped_column(String(16))
    author_id: Mapped[int | None] = mapped_column()
    text: Mapped[str | None] = mapped_column(Text)
    attachment_file_id: Mapped[str | None] = mapped_column(String(255))
    attachment_kind: Mapped[str | None] = mapped_column(String(16))


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_shop_created", "shop_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[int | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column()
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class ForcedChannel(Base, TimestampMixin):
    __tablename__ = "forced_channels"
    __table_args__ = (UniqueConstraint("shop_id", "chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    chat_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(96))
    url: Mapped[str] = mapped_column(String(255))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class LogChannel(Base, TimestampMixin):
    """Where each report topic goes (SUP-03): a chat and an optional forum thread."""

    __tablename__ = "log_channels"
    __table_args__ = (UniqueConstraint("shop_id", "topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[int] = mapped_column()
    thread_id: Mapped[int | None] = mapped_column()


class BotText(Base, TimestampMixin):
    __tablename__ = "bot_texts"
    __table_args__ = (UniqueConstraint("shop_id", "locale", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    locale: Mapped[str] = mapped_column(String(8))
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)


class SupportContact(Base, TimestampMixin):
    __tablename__ = "support_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(255))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class InboxDismissal(Base, TimestampMixin):
    __tablename__ = "inbox_dismissals"
    __table_args__ = (UniqueConstraint("staff_id", "item_type", "item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"))
    item_type: Mapped[str] = mapped_column(String(24))
    item_id: Mapped[int] = mapped_column()


class BroadcastPin(Base, TimestampMixin):
    """A broadcast pinned in a customer's chat, kept so it can be unpinned later (GRW-06)."""

    __tablename__ = "broadcast_pins"
    __table_args__ = (Index("ix_broadcast_pins_unpin", "shop_id", "unpin_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"))
    broadcast_id: Mapped[int | None] = mapped_column(ForeignKey("broadcasts.id", ondelete="SET NULL"))
    chat_id: Mapped[int] = mapped_column()
    message_id: Mapped[int] = mapped_column()
    unpin_at: Mapped[datetime | None] = mapped_column()
