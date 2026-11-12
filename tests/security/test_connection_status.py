"""The live connection page: every panel of the shop with its health, response time now and on
average, uptime, the recent checks oldest first, and its nodes; another shop's panels never show.
A "check all now" runs the probes at once, but not more than once in a short while."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.db.base import utcnow
from paurubot.db.models import PanelCheck
from paurubot.provisioning import monitor
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_the_page_shows_health_latency_history_and_nodes(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    now = utcnow()
    for minutes, ok, ms in ((50, True, 100), (40, False, None), (30, True, 300), (20, True, 200)):
        session.add(
            PanelCheck(
                panel_id=env.panel.id,
                checked_at=now - timedelta(minutes=minutes),
                ok=ok,
                latency_ms=ms,
                error=None if ok else "timeout",
            )
        )
    session.add(
        PanelCheck(panel_id=env.panel.id, checked_at=now - timedelta(days=3), ok=True, latency_ms=900)
    )
    session.add(PanelCheck(panel_id=env.other_panel.id, checked_at=now, ok=True, latency_ms=5))
    env.panel.is_healthy = True
    env.panel.health_checked_at = now - timedelta(minutes=20)
    env.panel.stats = {
        "nodes": [
            {"id": 1, "name": "de-1", "status": "connected"},
            {"id": 2, "name": "de-2", "status": "error", "message": "core stopped"},
        ]
    }
    await session.commit()

    owner = await login(http, "u_owner")
    resp = await owner.call("GET", "/api/admin/connection")
    assert resp.status_code == 200, resp.text
    panels = resp.json()["panels"]
    assert [p["id"] for p in panels] == [env.panel.id], "only this shop's panels"
    row = panels[0]
    assert row["healthy"] is True
    assert row["latency_ms"] == 200, "the last good check"
    assert row["latency_avg"] == 200, "the good checks of the last day: (100 + 300 + 200) / 3"
    assert [h["ok"] for h in row["history"]][-4:] == [True, False, True, True], "oldest first"
    assert row["history"][-1]["ms"] == 200
    assert row["uptime"]["day"] == 75.0
    assert row["nodes"] == [
        {"name": "de-1", "status": "connected", "ok": True, "message": None},
        {"name": "de-2", "status": "error", "ok": False, "message": "core stopped"},
    ]
    assert "base_url" not in row and "username" not in row, "no connection details on this page"


async def test_staff_without_panel_access_cannot_see_it(http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    support = await login(http, "u_support")
    assert (await support.call("GET", "/api/admin/connection")).status_code == 403
    assert (await support.call("POST", "/api/admin/connection/check")).status_code == 403


async def test_check_all_runs_once_in_a_while(session, http, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    probed: list[int] = []

    async def fake_check(db, panel, now=None):  # type: ignore[no-untyped-def]
        probed.append(panel.id)
        return monitor.CheckResult(True, 42, None, [])

    monkeypatch.setattr(monitor, "check_panel", fake_check)
    owner = await login(http, "u_owner")
    resp = await owner.call("POST", "/api/admin/connection/check")
    assert resp.status_code == 200, resp.text
    assert probed == [env.panel.id], "only this shop's active panels"
    assert "panels" in resp.json()
    again = await owner.call("POST", "/api/admin/connection/check")
    assert again.status_code == 429
    assert again.json()["error"] == "connection_check_too_soon"
    assert probed == [env.panel.id]
