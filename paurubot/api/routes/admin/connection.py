"""Live connection status (ClockBot parity B4): every panel of the shop and its nodes, with the
response time now and on average, uptime, and the recent checks, from the monitor's own probes
(PNL-12). Nothing here reaches a panel except "check all now", which runs the same probe."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from paurubot import kv
from paurubot.api.deps import DB, StaffContext, require
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, PanelCheck

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("panels.read"))]

HISTORY = 48  # checks per panel on the page, oldest first
CHECK_ALL_EVERY = 30  # seconds between two "check all now" runs of one shop


def _nodes(stats: dict[str, Any] | None) -> list[dict[str, Any]]:
    from paurubot.provisioning.monitor import NODE_OK

    out = []
    for node in (stats or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        status = str(node.get("status") or "unknown")
        out.append(
            {
                "name": str(node.get("name") or node.get("id") or "?")[:64],
                "status": status[:32],
                "ok": status.lower() in NODE_OK,
                "message": (str(node["message"])[:200] if node.get("message") else None),
            }
        )
    return out


async def _rows(db: DB, shop_id: int) -> list[dict[str, Any]]:
    from paurubot.provisioning import monitor

    panels = list(await db.scalars(select(Panel).where(Panel.shop_id == shop_id).order_by(Panel.id)))
    ids = [p.id for p in panels]
    if not ids:
        return []
    since = utcnow() - timedelta(days=1)
    averages: dict[int, Any] = dict(
        (
            await db.execute(
                select(PanelCheck.panel_id, func.avg(PanelCheck.latency_ms))
                .where(
                    PanelCheck.panel_id.in_(ids),
                    PanelCheck.ok.is_(True),
                    PanelCheck.checked_at >= since,
                )
                .group_by(PanelCheck.panel_id)
            )
        ).all()
    )
    ranked = (
        select(
            PanelCheck.panel_id,
            PanelCheck.checked_at,
            PanelCheck.ok,
            PanelCheck.latency_ms,
            func.row_number()
            .over(partition_by=PanelCheck.panel_id, order_by=PanelCheck.checked_at.desc())
            .label("n"),
        )
        .where(PanelCheck.panel_id.in_(ids))
        .subquery()
    )
    history: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for panel_id, at, ok, ms in (
        await db.execute(
            select(ranked.c.panel_id, ranked.c.checked_at, ranked.c.ok, ranked.c.latency_ms)
            .where(ranked.c.n <= HISTORY)
            .order_by(ranked.c.panel_id, ranked.c.checked_at)
        )
    ).all():
        history[int(panel_id)].append({"at": at.isoformat(), "ok": bool(ok), "ms": ms})
    uptime = await monitor.uptime(db, ids)
    rows = []
    for panel in panels:
        checks = history.get(panel.id, [])
        last_ok = next((c["ms"] for c in reversed(checks) if c["ok"]), None)
        average = averages.get(panel.id)
        rows.append(
            {
                "id": panel.id,
                "name": panel.name,
                "kind": panel.kind.value,
                "location": panel.location.title if panel.location is not None else None,
                "active": panel.is_active,
                "healthy": panel.is_healthy,
                "error": panel.health_error,
                "checked_at": panel.health_checked_at.isoformat() if panel.health_checked_at else None,
                "down_since": panel.down_since.isoformat() if panel.down_since else None,
                "latency_ms": last_ok,
                "latency_avg": round(float(average)) if average is not None else None,
                "uptime": uptime.get(panel.id),
                "history": checks,
                "nodes": _nodes(panel.stats),
            }
        )
    return rows


@router.get("/connection")
async def connection(ctx: Reader, db: DB) -> dict[str, Any]:
    return {"panels": await _rows(db, ctx.shop_id), "at": utcnow().isoformat()}


@router.post("/connection/check")
async def check_all(ctx: Reader, db: DB) -> dict[str, Any]:
    """Probe every active panel of the shop now, at most once per CHECK_ALL_EVERY seconds."""
    from paurubot.provisioning import monitor

    if not await kv.set_once(f"connection:check:{ctx.shop_id}", CHECK_ALL_EVERY):
        raise HTTPException(status_code=429, detail="connection_check_too_soon")
    panels = list(
        await db.scalars(
            select(Panel).where(Panel.shop_id == ctx.shop_id, Panel.is_active.is_(True)).order_by(Panel.id)
        )
    )
    for panel in panels:
        await monitor.check_panel(db, panel)
        await db.commit()
    return {"panels": await _rows(db, ctx.shop_id), "at": utcnow().isoformat()}
