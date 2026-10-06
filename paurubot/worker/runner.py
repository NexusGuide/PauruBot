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
    """Raise from a handler to retry later without logging a traceback."""


class Permanent(Exception):
    """Raise from a handler when retrying cannot help."""


@dataclass(slots=True)
class QueueSpec:
    handlers: dict[str, Handler]
    on_dead: DeadHook | None = None
    batch: int = 10
    lease_seconds: int = 120


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
    handler = spec.handlers.get(job.kind)
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            if handler is None:
                raise Permanent(f"no handler for {name}/{job.kind}")
            await handler(session, job)
            await queue.complete(session, job)
            await session.commit()
            return
        except Retry as exc:
            error, retry = str(exc) or "retry", True
        except Permanent as exc:
            error, retry = str(exc), False
        except Exception as exc:
            error, retry = f"{exc.__class__.__name__}: {exc}", True
            log.exception("job %s %s/%s failed", job.id, name, job.kind)
        await session.rollback()
    error = redact(error)
    async with maker() as session:
        dead = await queue.fail(session, job, error, retry=retry)
        await session.commit()
        if dead and spec.on_dead is not None:
            try:
                await spec.on_dead(session, job, error)
                await session.commit()
            except Exception:
                log.exception("dead-letter hook failed for job %s", job.id)


async def drain(name: str, spec: QueueSpec) -> int:
    async with get_sessionmaker()() as session:
        jobs = await queue.claim(session, name, limit=spec.batch, lease_seconds=spec.lease_seconds)
    if jobs:
        await asyncio.gather(*(run_job(name, spec, job) for job in jobs))
    return len(jobs)


async def run_periodic(task: Periodic) -> None:
    try:
        async with kv.lock(f"periodic:{task.name}", ttl=max(30, int(task.every))):
            await task.func()
    except kv.LockNotAcquired:
        return  # another worker is running it
    except Exception:
        log.exception("periodic task %s failed", task.name)


class Worker:
    def __init__(self, idle_sleep: float = 1.0) -> None:
        self.idle_sleep = idle_sleep
        self._stop = asyncio.Event()

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
                    asyncio.create_task(run_periodic(task))
            if not busy:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.idle_sleep)
                except TimeoutError:
                    pass
        log.info("worker stopped")
