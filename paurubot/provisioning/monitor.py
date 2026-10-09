"""Panel and node uptime monitoring with alerts (PNL-12), live stats (PNL-13) and quota (PNL-02)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, case, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, PanelCheck
from paurubot.panels import PanelError, driver_for
from paurubot.worker import queue

log = logging.getLogger(__name__)

FAILS_BEFORE_ALERT = 2
KEEP_CHECKS = timedelta(days=14)
QUOTA_WARN_RATIO = 0.9
NODE_OK = {"connected", "online", "healthy"}


@dataclass(slots=True)
class CheckResult:
    ok: bool
    latency_ms: int | None
    error: str | None
    events: list[str]


def quota_full(panel: Panel) -> bool:
    """The bot's own panel admin has no user or traffic quota left (PNL-02)."""
    admin = (panel.stats or {}).get("admin") or {}
    if admin.get("disabled"):
        return True
    users, max_users = admin.get("users"), admin.get("max_users")
    if max_users and users is not None and users >= max_users:
        return True
    used, limit = admin.get("used_traffic"), admin.get("data_limit")
    return bool(limit and used is not None and used >= limit)


def _node_states(stats: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    return {
        str(n.get("id")): (str(n.get("name") or n.get("id")), str(n.get("status") or "").lower() in NODE_OK)
        for n in stats.get("nodes") or []
        if isinstance(n, dict)
    }


def classify_panel_error(error: str | None) -> str:
    """Distinct failure reason for down alerts: token_invalid vs xray_stopped vs unreachable."""
    text = (error or "").lower()
    if any(key in text for key in ("token", "unauthorized", "401", "api key", "login failed", "rejected")):
        return "token_invalid"
    if "xray" in text or "stopped" in text:
        return "xray_stopped"
    return "unreachable"


async def _alert(session: AsyncSession, panel: Panel, event: str, **extra: Any) -> None:
    await queue.enqueue(
        session,
        "notify",
        "panel_alert",
        {"panel_id": panel.id, "event": event, **extra},
        shop_id=panel.shop_id,
    )


async def _recent_failures(session: AsyncSession, panel_id: int, limit: int) -> int:
    rows = list(
        await session.scalars(
            select(PanelCheck.ok)
            .where(PanelCheck.panel_id == panel_id)
            .order_by(PanelCheck.checked_at.desc())
            .limit(limit)
        )
    )
    failures = 0
    for ok in rows:
        if ok:
            break
        failures += 1
    return failures


async def check_panel(session: AsyncSession, panel: Panel, now: datetime | None = None) -> CheckResult:
    """Probe one panel, store the check and stats, and queue alerts for state changes. Caller commits."""
    now = now or utcnow()
    started = time.monotonic()
    error: str | None = None
    stats: dict[str, Any] | None = None
    try:
        stats = await (await driver_for(panel)).stats()
    except PanelError as exc:
        error = str(exc)[:255] or exc.__class__.__name__
    latency = int((time.monotonic() - started) * 1000)
    ok = stats is not None
    session.add(
        PanelCheck(panel_id=panel.id, checked_at=now, ok=ok, latency_ms=latency if ok else None, error=error)
    )
    await session.flush()
    events: list[str] = []
    panel.health_checked_at = now
    if ok:
        assert stats is not None
        previous = dict(panel.stats or {})
        panel.stats = stats
        panel.stats_at = now
        panel.is_healthy = True
        panel.health_error = None
        if panel.down_since is not None:
            minutes = max(1, int((now - panel.down_since).total_seconds() // 60))
            await _alert(session, panel, "up", minutes=minutes)
            events.append("up")
            panel.down_since = None
        before, after = _node_states(previous), _node_states(stats)
        for node_id, (name, healthy) in after.items():
            was = before.get(node_id)
            if was is not None and was[1] and not healthy:
                await _alert(session, panel, "node_down", node=name)
                events.append(f"node_down:{name}")
            elif was is not None and not was[1] and healthy:
                await _alert(session, panel, "node_up", node=name)
                events.append(f"node_up:{name}")
        events += await _quota_warning(session, panel)
    else:
        panel.health_error = error
        failures = await _recent_failures(session, panel.id, FAILS_BEFORE_ALERT)
        if failures >= FAILS_BEFORE_ALERT and panel.down_since is None:
            panel.down_since = now
            panel.is_healthy = False
            reason = classify_panel_error(error)
            await _alert(
                session, panel, "down", error=f"{reason}: {error}" if error else reason, reason=reason
            )
            events.append("down")
    return CheckResult(ok, latency if ok else None, error, events)


async def _quota_warning(session: AsyncSession, panel: Panel) -> list[str]:
    admin = (panel.stats or {}).get("admin") or {}
    ratios = []
    if admin.get("max_users") and admin.get("users") is not None:
        ratios.append(admin["users"] / admin["max_users"])
    if admin.get("data_limit") and admin.get("used_traffic") is not None:
        ratios.append(admin["used_traffic"] / admin["data_limit"])
    if not ratios or max(ratios) < QUOTA_WARN_RATIO:
        return []
    if not await kv.set_once(f"panel:{panel.id}:quota", 86_400):
        return []
    await _alert(session, panel, "quota", percent=int(max(ratios) * 100))
    return ["quota"]


async def run(session: AsyncSession) -> int:
    """Check every active panel. Commits per panel; returns how many were checked."""
    ids = list(await session.scalars(select(Panel.id).where(Panel.is_active.is_(True)).order_by(Panel.id)))
    for panel_id in ids:
        panel = await session.get(Panel, panel_id)
        if panel is None:
            continue
        try:
            await check_panel(session, panel)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("monitoring panel %s failed", panel_id)
    await session.execute(delete(PanelCheck).where(PanelCheck.checked_at < utcnow() - KEEP_CHECKS))
    await session.commit()
    return len(ids)


async def uptime(session: AsyncSession, panel_ids: list[int]) -> dict[int, dict[str, float | None]]:
    """Share of successful checks over the last day and week, per panel."""
    if not panel_ids:
        return {}
    now = utcnow()
    out: dict[int, dict[str, float | None]] = {pid: {"day": None, "week": None} for pid in panel_ids}
    for label, since in (("day", now - timedelta(days=1)), ("week", now - timedelta(days=7))):
        rows = await session.execute(
            select(
                PanelCheck.panel_id,
                func.count(PanelCheck.id),
                func.sum(cast(case((PanelCheck.ok, 1), else_=0), Integer)),
            )
            .where(PanelCheck.panel_id.in_(panel_ids), PanelCheck.checked_at >= since)
            .group_by(PanelCheck.panel_id)
        )
        for panel_id, total, good in rows.all():
            if total:
                out[int(panel_id)][label] = round(100.0 * int(good or 0) / int(total), 2)
    return out
