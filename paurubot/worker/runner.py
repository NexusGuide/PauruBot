"""Worker process: drains durable queues and runs periodic tasks (JOB-01)."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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


CRON_PROTECTED: frozenset[str] = frozenset({"worker.heartbeat"})
CRON_MIN_INTERVAL = 30
CRON_MAX_INTERVAL = 7 * 86400
CRON_STATE_PREFIX = "cron:state:"
CRON_RUN_NOW_PREFIX = "cron:run_now:"
CRON_STATE_TTL = 14 * 86400


async def _record_cron_state(name: str, *, status: str, duration_ms: int, error: str | None = None) -> None:
    import json
    from datetime import UTC, datetime

    try:
        redis = kv.get_redis()
        key = f"{CRON_STATE_PREFIX}{name}"
        prev_raw = await redis.get(key)
        count = 0
        if prev_raw:
            try:
                count = int(json.loads(prev_raw).get("run_count") or 0)
            except Exception:
                count = 0
        payload = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "last_duration_ms": duration_ms,
            "last_status": status,
            "last_error": redact(error)[:240] if error else None,
            "run_count": count + 1,
        }
        await redis.set(key, json.dumps(payload), ex=CRON_STATE_TTL)
    except Exception:
        log.debug("cron state write failed for %s", name, exc_info=True)


async def get_cron_states(names: list[str]) -> dict[str, dict[str, Any]]:
    import json

    if not names:
        return {}
    try:
        raw = await kv.get_redis().mget([f"{CRON_STATE_PREFIX}{n}" for n in names])
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, item in zip(names, raw, strict=True):
        if item:
            try:
                parsed = json.loads(item)
                if isinstance(parsed, dict):
                    out[name] = parsed
            except Exception:
                pass
    return out


async def request_cron_run(name: str) -> None:
    await kv.get_redis().set(f"{CRON_RUN_NOW_PREFIX}{name}", "1", ex=300)


async def _notify_cron_failure(name: str, error: str) -> None:
    mark_key = f"cron-fail-notified:{name}"
    try:
        if await kv.marked([mark_key]):
            return
        await kv.mark(mark_key, 3600)
        from html import escape as esc

        from paurubot.core.shops import root_shop
        from paurubot.notifications import handlers as notify_handlers
        from paurubot.notifications import log_reports

        async with get_sessionmaker()() as session:
            shop = await root_shop(session)
            if shop is None:
                return
            t = await log_reports.shop_texts(session, shop.id)
            text = t("report_cron_failed", name=esc(name), error=esc(redact(error)[:240]))
            await notify_handlers.report(session, shop.id, "crons", text)
    except Exception:
        log.debug("cron failure notification skipped for %s", name, exc_info=True)


async def run_periodic(task: Periodic, *, interval_seconds: float | None = None) -> None:
    started = time.monotonic()
    ttl = max(30, int(interval_seconds or task.every))
    try:
        async with kv.lease(f"periodic:{task.name}", ttl=min(ttl, 3600)):
            await task.func()
        ms = int((time.monotonic() - started) * 1000)
        await _record_cron_state(task.name, status="ok", duration_ms=ms)
    except kv.LockNotAcquired:
        return  # another worker is running it
    except Exception as exc:
        ms = int((time.monotonic() - started) * 1000)
        err = f"{exc.__class__.__name__}: {exc}"
        await _record_cron_state(task.name, status="error", duration_ms=ms, error=err)
        log.exception("periodic task %s failed", task.name)
        await _notify_cron_failure(task.name, err)


# Jobs running at once across every queue: the largest batch the single loop ever ran, so the
# database pool sees no more concurrency than before (each job may hold a second, heartbeat session).
MAX_JOBS = 10
# On stop: how long running jobs and periodic tasks may finish before they are cancelled. Jobs
# cancelled after that keep their lease; it expires and another run picks them up (at-least-once).
SHUTDOWN_GRACE = 30.0


class Worker:
    """Each queue drains in its own loop and periodic tasks have their own scheduler (SEC-F16).

    A slow batch in one queue (a panel timing out) no longer holds up the other queues or the
    schedule. Job slots are shared and taken *before* claiming, so a claimed job never waits for
    a slot while its lease runs down.
    """

    def __init__(
        self, idle_sleep: float = 1.0, *, max_jobs: int = MAX_JOBS, grace: float = SHUTDOWN_GRACE
    ) -> None:
        self.idle_sleep = idle_sleep
        self.grace = grace
        self._stop = asyncio.Event()
        self._slots = asyncio.Semaphore(max_jobs)
        # The loop keeps only weak references to tasks; hold periodic runs until they finish.
        self._running: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stop.set()

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _drain(self, name: str, spec: QueueSpec) -> int:
        """Claim up to `spec.batch` jobs, no more than free slots, and run them. Returns the count."""
        await self._slots.acquire()
        taken = 1
        while taken < spec.batch and not self._slots.locked():
            await self._slots.acquire()  # a free slot: returns at once
            taken += 1
        jobs: list[queue.Claimed] = []
        try:
            if not self._stop.is_set():
                async with get_sessionmaker()() as session:
                    jobs = await queue.claim(session, name, limit=taken, lease_seconds=spec.lease_seconds)
        finally:
            for _ in range(taken - len(jobs)):
                self._slots.release()

        async def one(job: queue.Claimed) -> None:
            try:
                await run_job(name, spec, job)
            finally:
                self._slots.release()

        if jobs:
            await asyncio.gather(*(one(job) for job in jobs))
        return len(jobs)

    async def _queue_loop(self, name: str, spec: QueueSpec) -> None:
        while not self._stop.is_set():
            try:
                ran = await self._drain(name, spec)
            except Exception:
                log.exception("draining %s failed", name)
                ran = 0
            if not ran:
                await self._idle(self.idle_sleep)

    async def _load_cron_overrides(self) -> dict[str, dict[str, Any]]:
        try:
            from paurubot.core import settings as shop_settings
            from paurubot.core.shops import root_shop

            async with get_sessionmaker()() as session:
                shop = await root_shop(session)
                if shop is None:
                    return {}
                cfg = await shop_settings.load(session, shop.id)
                raw = cfg.get("cron_overrides")
                return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    async def _scheduler(self) -> None:
        overrides: dict[str, dict[str, Any]] = {}
        overrides_until = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= overrides_until:
                overrides = await self._load_cron_overrides()
                overrides_until = now + 15.0
            manual_hits: set[str] = set()
            try:
                redis = kv.get_redis()
                keys = [f"{CRON_RUN_NOW_PREFIX}{t.name}" for t in PERIODIC]
                if keys:
                    vals = await redis.mget(keys)
                    for t, v in zip(PERIODIC, vals, strict=True):
                        if v:
                            manual_hits.add(t.name)
                            await redis.delete(f"{CRON_RUN_NOW_PREFIX}{t.name}")
            except Exception:
                pass
            for task in PERIODIC:
                raw_ov = overrides.get(task.name)
                ov: dict[str, Any] = raw_ov if isinstance(raw_ov, dict) else {}
                enabled = True if task.name in CRON_PROTECTED else bool(ov.get("enabled", True))
                raw_int = ov.get("interval_seconds")
                eff_every = (
                    float(raw_int)
                    if isinstance(raw_int, int) and CRON_MIN_INTERVAL <= raw_int <= CRON_MAX_INTERVAL
                    else task.every
                )
                manual = task.name in manual_hits
                if not manual and not enabled:
                    task.next_run = max(task.next_run, now + eff_every)
                    continue
                if manual or now >= task.next_run:
                    task.next_run = now + eff_every
                    running = asyncio.create_task(
                        run_periodic(task, interval_seconds=eff_every),
                        name=f"periodic:{task.name}",
                    )
                    self._running.add(running)
                    running.add_done_callback(self._running.discard)
            await self._idle(min(self.idle_sleep, 1.0))

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, RuntimeError):
                pass
        log.info("worker started: queues=%s periodic=%s", sorted(QUEUES), [p.name for p in PERIODIC])
        loops = [asyncio.create_task(self._queue_loop(n, s), name=f"queue:{n}") for n, s in QUEUES.items()]
        loops.append(asyncio.create_task(self._scheduler(), name="scheduler"))
        try:
            await self._stop.wait()
            log.info("worker stopping: no new claims; finishing running work")
            await self._finish([*loops, *self._running])
        except BaseException:
            # Cancelled from outside (or a crash): never leave loops or periodic runs behind.
            self._stop.set()
            await self._finish([*loops, *self._running], grace=0)
            raise
        log.info("worker stopped")

    async def _finish(self, tasks: list[asyncio.Task[None]], grace: float | None = None) -> None:
        live = [task for task in tasks if not task.done()]
        if not live:
            return
        _, late = await asyncio.wait(live, timeout=self.grace if grace is None else grace)
        if late:
            log.warning("cancelling %s task(s) still running after the shutdown grace", len(late))
            for task in late:
                task.cancel()
            await asyncio.gather(*late, return_exceptions=True)
