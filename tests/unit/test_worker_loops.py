"""SEC-F16: queues and the scheduler run independently, within shared job slots, and stop cleanly.

Before, one loop drained the queues one after another and waited for each whole batch. A slow
batch (a panel timing out) held up every other queue and the periodic schedule, and on stop the
running periodic tasks were simply abandoned.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.db import session as db_session
from paurubot.db.models import Job, JobStatus
from paurubot.worker import queue, runner
from paurubot.worker.runner import Periodic, QueueSpec, Worker


@pytest.fixture
async def world(engine, monkeypatch):  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    db_session.set_sessionmaker(maker)
    async with maker() as s:  # earlier tests may have committed jobs; keep only ours
        await s.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
        await s.commit()
    monkeypatch.setattr(runner, "QUEUES", {})
    monkeypatch.setattr(runner, "PERIODIC", [])
    yield maker
    db_session.set_sessionmaker(None)  # type: ignore[arg-type]


class Recorder:
    def __init__(self) -> None:
        self.started: dict[str, float] = {}
        self.done: dict[str, float] = {}
        self.running = self.peak = 0

    def handler(self, seconds: float):  # type: ignore[no-untyped-def]
        async def run(_session, job: queue.Claimed) -> None:  # type: ignore[no-untyped-def]
            tag = str(job.payload["tag"])
            self.started[tag] = time.monotonic()
            self.running += 1
            self.peak = max(self.peak, self.running)
            try:
                await asyncio.sleep(seconds)
            finally:
                self.running -= 1
            self.done[tag] = time.monotonic()

        return run


async def _enqueue(maker, name: str, tags: list[str]) -> None:  # type: ignore[no-untyped-def]
    async with maker() as s:
        for tag in tags:
            await queue.enqueue(s, name, "work", {"tag": tag})
        await s.commit()


async def _until(predicate, limit: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + limit
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


async def test_a_slow_queue_does_not_hold_up_another_queue_or_the_schedule(world) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder()
    runner.QUEUES["slowq"] = QueueSpec({"work": rec.handler(2.0)}, batch=2)
    runner.QUEUES["fastq"] = QueueSpec({"work": rec.handler(0.0)}, batch=5)
    ticks: list[float] = []

    async def tick() -> None:
        ticks.append(time.monotonic())

    runner.PERIODIC.append(Periodic("test.tick", 0.2, tick))
    await _enqueue(world, "slowq", ["s1", "s2"])
    worker = Worker(idle_sleep=0.05, grace=5)
    running = asyncio.create_task(worker.run())
    try:
        await _until(lambda: "s1" in rec.started and "s2" in rec.started)
        began = time.monotonic()
        await _enqueue(world, "fastq", ["f1", "f2", "f3"])
        await _until(lambda: {"f1", "f2", "f3"} <= rec.done.keys())
        assert time.monotonic() - began < 1.5  # served while the slow batch still runs
        # The schedule keeps ticking while the slow batch is still running.
        await _until(lambda: len([t for t in ticks if t >= began]) >= 2, limit=1.5)
        assert "s1" not in rec.done
    finally:
        worker.stop()
        await asyncio.wait_for(running, 10)
    assert {"s1", "s2"} <= rec.done.keys()  # the stop let the running batch finish


async def test_job_slots_are_shared_and_never_exceeded(world) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder()
    runner.QUEUES["aq"] = QueueSpec({"work": rec.handler(0.3)}, batch=5)
    runner.QUEUES["bq"] = QueueSpec({"work": rec.handler(0.3)}, batch=5)
    await _enqueue(world, "aq", [f"a{i}" for i in range(8)])
    await _enqueue(world, "bq", [f"b{i}" for i in range(8)])
    worker = Worker(idle_sleep=0.05, max_jobs=3, grace=5)
    running = asyncio.create_task(worker.run())
    try:
        await _until(lambda: len(rec.done) == 16, limit=15)
    finally:
        worker.stop()
        await asyncio.wait_for(running, 10)
    assert rec.peak == 3


async def test_stop_waits_for_running_work_then_cancels_after_the_grace(world) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder()
    runner.QUEUES["longq"] = QueueSpec({"work": rec.handler(30.0)}, batch=1)
    cancelled = asyncio.Event()

    async def long_periodic() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner.PERIODIC.append(Periodic("test.long", 3600, long_periodic))
    await _enqueue(world, "longq", ["l1"])
    worker = Worker(idle_sleep=0.05, grace=0.5)
    running = asyncio.create_task(worker.run())
    await _until(lambda: "l1" in rec.started)
    began = time.monotonic()
    worker.stop()
    await asyncio.wait_for(running, 5)
    assert time.monotonic() - began < 2.0  # bounded by the grace, not by the 30 s job
    assert cancelled.is_set()  # the periodic run was cancelled, not abandoned
    assert "l1" not in rec.done
