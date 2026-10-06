"""Prometheus metrics (JOB-05): job outcomes counted in Redis, gauges read from the database.

Every process (api, bot, worker) records into Redis, so the API can expose one view without a
metrics sidecar or multiprocess files.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Order, OrderStatus, Panel, Payment, PaymentStatus, Service

log = logging.getLogger(__name__)

PREFIX = "metrics:"


async def count_job(queue: str, outcome: str) -> None:
    try:
        await kv.get_redis().hincrby(f"{PREFIX}jobs", f"{queue}|{outcome}", 1)
    except Exception:  # noqa: BLE001  metrics must never break a job
        log.debug("metrics write failed", exc_info=True)


async def count_http(method: str, route: str, status: int, seconds: float) -> None:
    try:
        redis = kv.get_redis()
        key = f"{method}|{route}|{status // 100}xx"
        async with redis.pipeline(transaction=False) as pipe:
            pipe.hincrby(f"{PREFIX}http", key, 1)
            pipe.hincrbyfloat(f"{PREFIX}http_seconds", key, seconds)
            await pipe.execute()
    except Exception:  # noqa: BLE001
        log.debug("metrics write failed", exc_info=True)


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _line(metric: str, value: float, **labels: object) -> str:
    if labels:
        inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        return f"{metric}{{{inner}}} {value}"
    return f"{metric} {value}"


async def render(session: AsyncSession) -> str:
    out: list[str] = []

    def header(name: str, kind: str, text: str) -> None:
        out.append(f"# HELP {name} {text}")
        out.append(f"# TYPE {name} {kind}")

    redis = kv.get_redis()
    header("paurubot_jobs_total", "counter", "Finished job attempts by queue and outcome.")
    for key, value in sorted((await redis.hgetall(f"{PREFIX}jobs")).items()):
        queue_name, _, outcome = str(key).partition("|")
        out.append(_line("paurubot_jobs_total", float(value), queue=queue_name, outcome=outcome))
    header("paurubot_http_requests_total", "counter", "API requests by method, route and status class.")
    seconds = await redis.hgetall(f"{PREFIX}http_seconds")
    for key, value in sorted((await redis.hgetall(f"{PREFIX}http")).items()):
        method, route, status = str(key).split("|", 2)
        out.append(
            _line("paurubot_http_requests_total", float(value), method=method, route=route, status=status)
        )
    header("paurubot_http_request_seconds_total", "counter", "Time spent answering API requests.")
    for key, value in sorted(seconds.items()):
        method, route, status = str(key).split("|", 2)
        out.append(
            _line(
                "paurubot_http_request_seconds_total", float(value), method=method, route=route, status=status
            )
        )

    header("paurubot_jobs", "gauge", "Jobs by queue and status.")
    rows = await session.execute(
        select(Job.queue, Job.status, func.count(Job.id))
        .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DEAD]))
        .group_by(Job.queue, Job.status)
    )
    for queue_name, status, count in rows.all():
        out.append(_line("paurubot_jobs", int(count), queue=queue_name, status=status.value))
    header("paurubot_job_oldest_due_seconds", "gauge", "Age of the oldest due job per queue.")
    oldest_rows = await session.execute(
        select(Job.queue, func.min(Job.run_at))
        .where(Job.status == JobStatus.QUEUED, Job.run_at <= utcnow())
        .group_by(Job.queue)
    )
    now = utcnow()
    for queue_name, oldest in oldest_rows.all():
        out.append(
            _line(
                "paurubot_job_oldest_due_seconds", round((now - oldest).total_seconds(), 1), queue=queue_name
            )
        )

    header("paurubot_services", "gauge", "Services by status.")
    for status, count in (
        await session.execute(select(Service.status, func.count(Service.id)).group_by(Service.status))
    ).all():
        out.append(_line("paurubot_services", int(count), status=status.value))
    header("paurubot_panel_up", "gauge", "1 when the last probe of an active panel succeeded.")
    for panel_id, name, healthy in (
        await session.execute(select(Panel.id, Panel.name, Panel.is_healthy).where(Panel.is_active.is_(True)))
    ).all():
        out.append(_line("paurubot_panel_up", 1 if healthy else 0, panel=panel_id, name=name))
    header("paurubot_payments_waiting", "gauge", "Payments waiting for the customer or for review.")
    for status, count in (
        await session.execute(
            select(Payment.status, func.count(Payment.id))
            .where(Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.VERIFYING]))
            .group_by(Payment.status)
        )
    ).all():
        out.append(_line("paurubot_payments_waiting", int(count), status=status.value))
    header("paurubot_orders_completed_24h", "gauge", "Orders completed in the last 24 hours.")
    day = await session.scalar(
        select(func.count(Order.id)).where(
            Order.status == OrderStatus.COMPLETED, Order.completed_at >= now - timedelta(days=1)
        )
    )
    out.append(_line("paurubot_orders_completed_24h", int(day or 0)))
    return "\n".join(out) + "\n"
