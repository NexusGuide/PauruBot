"""Durable job queue on PostgreSQL with leases, retries and exponential backoff (JOB-01, SVC-10)."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import affected, utcnow
from paurubot.db.models import Job, JobStatus
from paurubot.security.tokens import new_token


async def enqueue(
    session: AsyncSession,
    queue: str,
    kind: str,
    payload: dict[str, Any],
    *,
    shop_id: int | None = None,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 8,
) -> int | None:
    """Add a job in the caller's transaction. With a dedupe key, a duplicate enqueue is a no-op."""
    from paurubot.logs import correlation_id

    cid = correlation_id.get()
    if cid and "_cid" not in payload:
        payload = {**payload, "_cid": cid}  # follow one request through the jobs it starts (JOB-05)
    stmt = insert(Job).values(
        shop_id=shop_id,
        queue=queue,
        kind=kind,
        payload=payload,
        status=JobStatus.QUEUED,
        run_at=run_at or utcnow(),
        max_attempts=max_attempts,
        dedupe_key=dedupe_key,
        attempts=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    return await session.scalar(stmt.returning(Job.id))


@dataclass(slots=True)
class Claimed:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    shop_id: int | None
    token: str


async def claim(
    session: AsyncSession, queue: str, *, limit: int = 10, lease_seconds: int = 120
) -> list[Claimed]:
    """Lease up to `limit` due jobs. Expired leases (crashed workers) become claimable again."""
    now = utcnow()
    rows = (
        await session.scalars(
            select(Job)
            .where(
                Job.queue == queue,
                Job.run_at <= now,
                or_(
                    Job.status == JobStatus.QUEUED,
                    (Job.status == JobStatus.RUNNING) & (Job.lease_until < now),
                ),
            )
            .order_by(Job.run_at, Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claimed: list[Claimed] = []
    for job in rows:
        token = new_token(16)
        job.status = JobStatus.RUNNING
        job.claim_token = token
        job.lease_until = now + timedelta(seconds=lease_seconds)
        job.attempts += 1
        claimed.append(
            Claimed(
                job.id, job.kind, dict(job.payload or {}), job.attempts, job.max_attempts, job.shop_id, token
            )
        )
    await session.commit()
    return claimed


async def complete(session: AsyncSession, job: Claimed) -> bool:
    result = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.claim_token == job.token)
        .values(status=JobStatus.DONE, finished_at=utcnow(), lease_until=None, last_error=None)
    )
    return bool(affected(result))


async def update_payload(session: AsyncSession, job: Claimed, payload: dict[str, Any]) -> None:
    """Save progress of a long job, so a retry continues where it stopped."""
    await session.execute(
        update(Job).where(Job.id == job.id, Job.claim_token == job.token).values(payload=payload)
    )


def backoff(attempt: int, base: float = 15.0, cap: float = 3600.0) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay * random.uniform(0.8, 1.2)


async def fail(session: AsyncSession, job: Claimed, error: str, *, retry: bool = True) -> bool:
    """Record a failure. Returns True when the job is dead (no more retries)."""
    dead = not retry or job.attempts >= job.max_attempts
    values: dict[str, Any] = {"last_error": error[:2000], "lease_until": None}
    if dead:
        values.update(status=JobStatus.DEAD, finished_at=utcnow())
    else:
        values.update(status=JobStatus.QUEUED, run_at=utcnow() + timedelta(seconds=backoff(job.attempts)))
    await session.execute(update(Job).where(Job.id == job.id, Job.claim_token == job.token).values(**values))
    return dead


async def retry_dead(session: AsyncSession, job_id: int) -> bool:
    result = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_([JobStatus.DEAD, JobStatus.FAILED]))
        .values(status=JobStatus.QUEUED, run_at=utcnow(), attempts=0, finished_at=None)
    )
    return bool(affected(result))
