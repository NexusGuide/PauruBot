"""Job tracing and dead-letter alerts (JOB-05), error forwarding (ARC-04) and the doctor (ADM-08)."""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select, update

from paurubot import kv
from paurubot.db.models import Job, JobStatus
from paurubot.logs import TelegramErrorHandler, correlation_id
from paurubot.ops import doctor
from paurubot.worker import queue
from paurubot.worker.runner import Permanent, QueueSpec, register_queue, run_job


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


seen: list[str | None] = []


async def _boom(session, job) -> None:  # type: ignore[no-untyped-def]
    seen.append(correlation_id.get())
    raise Permanent("panel said no")


register_queue("test_obs", QueueSpec({"boom": _boom}))


async def test_jobs_carry_the_request_id_and_dead_jobs_raise_an_alert(session) -> None:  # type: ignore[no-untyped-def]
    token = correlation_id.set("req-abc123")
    try:
        await queue.enqueue(session, "test_obs", "boom", {"x": 1})
    finally:
        correlation_id.reset(token)
    await session.commit()
    claimed = await queue.claim(session, "test_obs", limit=1)
    assert claimed[0].payload["_cid"] == "req-abc123"
    from paurubot.worker.runner import QUEUES

    await run_job("test_obs", QUEUES["test_obs"], claimed[0])
    assert seen[-1] == "req-abc123"
    alert = await session.scalar(select(Job).where(Job.kind == "job_dead", Job.status == JobStatus.QUEUED))
    assert (
        alert is not None
        and alert.payload["queue"] == "test_obs"
        and "panel said no" in alert.payload["error"]
    )
    counts = await kv.get_redis().hgetall("metrics:jobs")
    assert int(counts["test_obs|dead"]) >= 1


def test_error_logs_are_queued_for_telegram() -> None:
    handler = TelegramErrorHandler("worker")
    logger = logging.getLogger("paurubot.test.errors")
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("disk full token=SECRET123")
        except RuntimeError:
            logger.exception("backup failed")
    finally:
        logger.removeHandler(handler)
    import redis

    from paurubot.config import get_settings

    raw = redis.Redis.from_url(get_settings().redis_url).lrange(TelegramErrorHandler.KEY, -1, -1)
    entry = json.loads(raw[0])
    assert entry["process"] == "worker" and entry["msg"] == "backup failed"
    assert "RuntimeError" in entry["exc"]


async def test_doctor_reports_each_area() -> None:
    checks = {c.key: c for c in await doctor.run(with_telegram=False)}
    for key in (
        "configuration",
        "postgresql",
        "migrations",
        "redis",
        "worker",
        "dead_jobs",
        "disk",
        "backups",
    ):
        assert key in checks, key
    assert checks["postgresql"].level == "ok" and checks["migrations"].level == "ok"
    assert checks["worker"].level == "fail"  # no worker heartbeat in tests
    await doctor.heartbeat()
    checks = {c.key: c for c in await doctor.run(with_telegram=False)}
    assert checks["worker"].level == "ok"


async def test_pwa_files_are_served_as_files(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from paurubot.api.app import create_app

    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "index.html").write_text("<html>shell</html>")
    (tmp_path / "admin" / "manifest.webmanifest").write_text('{"name": "Pauru Admin"}')
    (tmp_path / "admin" / "secret.txt").write_text("nope")
    monkeypatch.setenv("FRONTEND_DIR", str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        manifest = await client.get("/admin/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert manifest.json()["name"] == "Pauru Admin"
        assert "shell" in (await client.get("/admin/secret.txt")).text  # unknown names get the app shell
        assert "shell" in (await client.get("/admin/customers/12")).text


def _public_frontend(into) -> list[tuple[str, str, bytes]]:  # type: ignore[no-untyped-def]
    """(folder, file name, bytes) of every public frontend file, copied next to a stub shell."""
    import shutil
    from pathlib import Path

    public = Path(__file__).resolve().parents[2] / "frontend" / "public"
    files = []
    for name in ("app", "admin"):
        shutil.copytree(public / name, into / name)
        (into / name / "index.html").write_text("<html>shell</html>")
        files += [(name, file.name, file.read_bytes()) for file in sorted((public / name).iterdir())]
    return files


async def test_every_public_frontend_file_is_served_as_itself(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A file under frontend/public/{app,admin} that fell back to the app shell would break the app.

    /app/launch.js did: served as index.html with nosniff, the browser refused it, so the Mini App
    had neither Telegram's SDK nor its launch data and could not sign in.
    """
    import httpx

    from paurubot.api.app import create_app
    from paurubot.api.static import PUBLIC_FILES

    files = _public_frontend(tmp_path)
    monkeypatch.setenv("FRONTEND_DIR", str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        for folder, name, body in files:
            assert name in PUBLIC_FILES, f"{folder}/{name} is not served as a file"
            response = await client.get(f"/{folder}/{name}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(PUBLIC_FILES[name]), name
            assert response.content == body, name


async def test_finished_jobs_follow_the_shop_log_retention(session) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from paurubot.core import settings as shop_settings
    from paurubot.db.base import utcnow
    from paurubot.worker.jobs import cleanup_jobs, retention_days
    from tests import factories

    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"log_retention_days": 3})
    now = utcnow()
    rows = [
        Job(
            shop_id=shop.id,
            queue="ops",
            kind="old",
            status=JobStatus.DONE,
            finished_at=now - timedelta(days=5),
        ),
        Job(
            shop_id=shop.id,
            queue="ops",
            kind="new",
            status=JobStatus.DONE,
            finished_at=now - timedelta(days=1),
        ),
        Job(
            shop_id=shop.id,
            queue="ops",
            kind="dead",
            status=JobStatus.DEAD,
            finished_at=now - timedelta(days=9),
        ),
    ]
    session.add_all(rows)
    await session.commit()
    await cleanup_jobs()
    kinds = set(await session.scalars(select(Job.kind).where(Job.shop_id == shop.id)))
    assert kinds == {"new", "dead"}
    assert (
        retention_days({"log_retention_days": 0}) == 14
        and retention_days({"log_retention_days": 9999}) == 365
    )
