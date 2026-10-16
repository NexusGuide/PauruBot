"""Real normalized panel-counter observations. Never reconstruct missing history."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import affected, utcnow
from paurubot.db.models import Service, ServiceUsage
from paurubot.panels.base import PanelUser, usage_counter

RETENTION_DAYS = 35
GAP_SECONDS = 2 * 3600


def period_key(service: Service) -> str:
    # Conservatively break on limit/expiry/identity changes, even a non-resetting add-on.
    expiry = service.expire_at.astimezone(UTC).isoformat() if service.expire_at else None
    fields = [service.panel_id, service.panel_username, service.data_limit, expiry]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


async def observe(
    session: AsyncSession,
    service: Service,
    info: PanelUser,
    *,
    source: Literal["poll", "webhook"],
    at: datetime | None = None,
) -> bool:
    value = usage_counter(info.used_bytes)
    if not info.usage_known or value is None:
        return False
    at = at or utcnow()
    if at.tzinfo is None:
        raise ValueError("usage observation needs an aware time")
    at = at.astimezone(UTC)
    bucket = at.replace(minute=0, second=0, microsecond=0)
    # Serialize history writes for one service, AFTER panel I/O. No network call under this lock.
    await session.scalar(select(Service.id).where(Service.id == service.id).with_for_update())
    previous = await session.scalar(
        select(ServiceUsage)
        .where(ServiceUsage.service_id == service.id)
        .order_by(ServiceUsage.observed_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if previous is not None and at <= previous.observed_at:
        return False  # late/replayed timestamps must not overwrite a newer observation
    key = period_key(service)
    boundary = previous is not None and (
        previous.period_key != key
        or value < previous.used_bytes
        or (at - previous.observed_at).total_seconds() > GAP_SECONDS
    )
    sticky = bool(previous and previous.bucket_at == bucket and previous.break_before)
    stmt = insert(ServiceUsage).values(
        service_id=service.id,
        bucket_at=bucket,
        observed_at=at,
        used_bytes=value,
        period_key=key,
        break_before=boundary or sticky,
        source=source,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ServiceUsage.service_id, ServiceUsage.bucket_at],
            set_={
                name: getattr(stmt.excluded, name)
                for name in ("observed_at", "used_bytes", "period_key", "break_before", "source")
            },
            where=ServiceUsage.observed_at < at,
        )
    )
    return True


async def series(
    session: AsyncSession, service: Service, days: int, *, now: datetime | None = None
) -> dict[str, Any]:
    if days not in (7, 30):
        raise ValueError("unsupported usage window")
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    rows = list(
        await session.scalars(
            select(ServiceUsage)
            .where(
                ServiceUsage.service_id == service.id,
                ServiceUsage.observed_at >= cutoff,
                ServiceUsage.observed_at <= now,
            )
            .order_by(ServiceUsage.observed_at)
            .limit(days * 24 + 1)
            .execution_options(populate_existing=True)
        )
    )
    points = []
    previous = None
    for row in rows:
        boundary = bool(row.break_before)
        if previous is not None:
            boundary = (
                boundary or row.period_key != previous.period_key or row.used_bytes < previous.used_bytes
            )
            boundary = boundary or (row.observed_at - previous.observed_at).total_seconds() > GAP_SECONDS
        points.append(
            {"at": row.observed_at.isoformat(), "used_bytes": row.used_bytes, "break_before": boundary}
        )
        previous = row
    return {
        "service_id": service.id,
        "days": days,
        "resolution": "hourly_last_observation",
        "from": cutoff.isoformat(),
        "to": now.isoformat(),
        "points": points,
        "last_observed_at": rows[-1].observed_at.isoformat() if rows else None,
    }


async def prune(session: AsyncSession, *, now: datetime | None = None, batch: int = 5000) -> int:
    cutoff = (now or utcnow()) - timedelta(days=RETENTION_DAYS)
    keys = (
        select(ServiceUsage.service_id, ServiceUsage.bucket_at)
        .where(ServiceUsage.bucket_at < cutoff)
        .limit(batch)
    )
    result = await session.execute(
        delete(ServiceUsage).where(tuple_(ServiceUsage.service_id, ServiceUsage.bucket_at).in_(keys))
    )
    return affected(result)
