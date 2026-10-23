"""Worker process: drains durable queues and runs periodic tasks (JOB-01)."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.session import get_sessionmaker
from paurubot.security.redact import redact
from paurubot.worker import queue

log = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, queue.Claimed], Awaitable[None]]
DeadHook = Callable[[AsyncSession, queue.Claimed, str], Awaitable[None]]


class Retry(Exception):
    """Retry without a traceback, optionally respecting a provider's minimum wait."""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        import math

        super().__init__(message)
        if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("retry_after must be finite and non-negative")
        self.retry_after = retry_after


class Defer(Exception):
    """A service is busy: requeue without using a failure attempt or running dead hooks."""


class Permanent(Exception):
    """Raise from a handler when retrying cannot help."""


@dataclass(slots=True)
class QueueSpec:
    handlers: dict[str, Handler]
    on_dead: DeadHook | None = None
    batch: int = 10
    lease_seconds: int = 120


DEAD_HOOK_KIND = "__dead_hook__"
QUEUES: dict[str, QueueSpec] = {}


@dataclass(slots=True)
class Periodic:
    name: str
    every: float
    func: Callable[[], Awaitable[None]]
    next_run: float = 0.0


PERIODIC: list[Periodic] = []


def register_queue(name: str, spec: QueueSpec) -> None:
    QUEUES[name] = spec


def every(
    seconds: float, name: str | None = None
) -> Callable[[Callable[[], Awaitable[None]]], Callable[[], Awaitable[None]]]:
    def decorator(func: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        PERIODIC.append(Periodic(name or func.__name__, seconds, func))
        return func

    return decorator


async def run_job(name: str, spec: QueueSpec, job: queue.Claimed) -> None:
    from paurubot.logs import correlation_id, log_tags

    cid = str(job.payload.get("_cid") or f"job-{job.id}")
    cid_token = correlation_id.set(cid)
    tags_token = log_tags.set({"job": job.id, "queue": name, "kind": job.kind, "shop": job.shop_id})
    try:
        await _run_job(name, spec, job)
    finally:
        correlation_id.reset(cid_token)
        log_tags.reset(tags_token)


async def _with_heartbeat(
    handler: Handler, session: AsyncSession, spec: QueueSpec, job: queue.Claimed
) -> None:
    maker = get_sessionmaker()

    async def keep() -> None:
        while True:
            await asyncio.sleep(max(0.05, min(30.0, spec.lease_seconds / 3)))
            # Never use the handler's session concurrently; it may be in a remote call/transaction.
            async with maker() as heartbeat_session:
                alive = await queue.renew(heartbeat_session, job, lease_seconds=spec.lease_seconds)
                await heartbeat_session.commit()
            if not alive:
                raise queue.LostClaim(f"job {job.id}: lease lost")

    async def invoke() -> None:
        await handler(session, job)

    work = asyncio.create_task(invoke())
    heartbeat = asyncio.create_task(keep())
    try:
        await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        if heartbeat.done():
            raise queue.LostClaim(f"job {job.id}: heartbeat failed") from heartbeat.exception()
        await work
    finally:
        # Cancellation leaves durable panel intents for reconciliation, not permission to repeat.
        work.cancel()
        heartbeat.cancel()
        await asyncio.gather(work, heartbeat, return_exceptions=True)


async def _run_job(name: str, spec: QueueSpec, job: queue.Claimed) -> None:
    from paurubot.ops import metrics

    handler = spec.handlers.get(job.kind)
    if job.kind == DEAD_HOOK_KIND and spec.on_dead is not None:

        async def compensate(session: AsyncSession, claimed: queue.Claimed) -> None:
            original = claimed.payload
            failed = queue.Claimed(
                id=int(original["id"]),
                kind=str(original["kind"]),
                payload=dict(original["payload"]),
                attempts=int(original["attempts"]),
                max_attempts=int(original["max_attempts"]),
                shop_id=claimed.shop_id,
                token="",  # Never reuse a stale original job's claim token.
            )
            assert spec.on_dead is not None
            await spec.on_dead(session, failed, str(original["error"]))

        handler = compensate
    retry_after: float | None = None
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            if handler is None:
                raise Permanent(f"no handler for {name}/{job.kind}")
            if not await queue.renew(session, job, lease_seconds=spec.lease_seconds):
                raise queue.LostClaim(f"job {job.id}: claim expired before handler")
            await session.commit()
            await _with_heartbeat(handler, session, spec, job)
            if not await queue.complete(session, job):
                raise queue.LostClaim(f"job {job.id}: claim lost before completion")
            await session.commit()
            await metrics.count_job(name, "done")
            return
        except queue.LostClaim:
            await session.rollback()
            log.warning("job %s: lost claim; no completion/failure/dead hook", job.id)
            return
        except Defer as exc:
            await session.rollback()
            if await queue.defer(session, job, str(exc) or "service busy"):
                await session.commit()
            else:
                await session.rollback()
            return
        except Retry as exc:
            error, retry = str(exc) or "retry", True
            retry_after = exc.retry_after
        except Permanent as exc:
            error, retry = str(exc), False
        except Exception as exc:
            error, retry = f"{exc.__class__.__name__}: {exc}", True
            log.exception("job %s %s/%s failed", job.id, name, job.kind)
        await session.rollback()
    error = redact(error)
    async with maker() as session:
        dead = await queue.fail(session, job, error, retry=retry, retry_after=retry_after)
        if dead is None:
            await session.rollback()
            log.warning("job %s: stale failure discarded", job.id)
            return
        if dead and spec.on_dead is not None and job.kind != DEAD_HOOK_KIND:
            # Persist compensation intent with the DEAD transition, before the commit.
            # A crash cannot strand a paid order between those two operations.
            await queue.enqueue(
                session,
                name,
                DEAD_HOOK_KIND,
                {
                    "id": job.id,
                    "kind": job.kind,
                    "payload": job.payload,
                    "attempts": job.attempts,
                    "max_attempts": job.max_attempts,
                    "error": error,
                },
                shop_id=job.shop_id,
                dedupe_key=f"dead-hook:{job.id}",
            )
        if dead and name != "notify":
            await queue.enqueue(
                session,
                "notify",
                "job_dead",
                {"job_id": job.id, "queue": name, "kind": job.kind, "error": error[:300]},
                shop_id=job.shop_id,
                dedupe_key=f"job-dead-notify:{job.id}",
            )
        await session.commit()
        await metrics.count_job(name, "dead" if dead else "retry")


async def drain(name: str, spec: QueueSpec) -> int:
    async with get_sessionmaker()() as session:
        jobs = await queue.claim(session, name, limit=spec.batch, lease_seconds=spec.lease_seconds)
    if jobs:
        await asyncio.gather(*(run_job(name, spec, job) for job in jobs))
    return len(jobs)


async def run_periodic(task: Periodic) -> None:
    try:
        async with kv.lease(f"periodic:{task.name}", ttl=max(30, int(task.every))):
            await task.func()
    except kv.LockNotAcquired:
        return  # another worker is running it
    except Exception:
        log.exception("periodic task %s failed", task.name)


class Worker:
    def __init__(self, idle_sleep: float = 1.0) -> None:
        self.idle_sleep = idle_sleep
        self._stop = asyncio.Event()
        # The loop keeps only weak references to tasks; hold periodic runs until they finish.
        self._running: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):
                pass
        log.info("worker started: queues=%s periodic=%s", sorted(QUEUES), [p.name for p in PERIODIC])
        while not self._stop.is_set():
            busy = 0
            for name, spec in list(QUEUES.items()):
                try:
                    busy += await drain(name, spec)
                except Exception:
                    log.exception("draining %s failed", name)
            now = time.monotonic()
            for task in PERIODIC:
                if now >= task.next_run:
                    task.next_run = now + task.every
                    running = asyncio.create_task(run_periodic(task))
                    self._running.add(running)
                    running.add_done_callback(self._running.discard)
            if not busy:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.idle_sleep)
                except TimeoutError:
                    pass
        log.info("worker stopped")
