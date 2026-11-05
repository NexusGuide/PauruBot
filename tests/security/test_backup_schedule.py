"""The automatic backup schedule is set on the Backups page and followed by the worker (OPS-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.models import AuditLog
from paurubot.ops import backup_schedule
from paurubot.ops.backup_schedule import Schedule, due, effective
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def plan(**changes: Any) -> Schedule:
    base = {"enabled": True, "every_hours": 6, "hour": 4, "keep": 14, "telegram": True}
    return Schedule(**{**base, **changes})


def test_every_few_hours_runs_once_the_interval_has_passed() -> None:
    six = plan(every_hours=6)
    assert due(six, now=NOW, local_hour=15, newest=None)
    assert not due(six, now=NOW, local_hour=15, newest=NOW - timedelta(hours=3))
    assert due(six, now=NOW, local_hour=15, newest=NOW - timedelta(hours=6))
    # The worker looks every ten minutes: a look just before the mark counts, so no hour slips.
    assert due(six, now=NOW, local_hour=15, newest=NOW - timedelta(hours=5, minutes=51))
    assert due(plan(every_hours=1), now=NOW, local_hour=15, newest=NOW - timedelta(minutes=55))


def test_daily_and_longer_run_at_the_set_hour() -> None:
    daily = plan(every_hours=24, hour=4)
    assert not due(daily, now=NOW, local_hour=15, newest=None)
    assert due(daily, now=NOW, local_hour=4, newest=NOW - timedelta(hours=24))
    assert not due(daily, now=NOW, local_hour=4, newest=NOW - timedelta(minutes=20))
    weekly = plan(every_hours=168, hour=4)
    assert not due(weekly, now=NOW, local_hour=4, newest=NOW - timedelta(days=2))
    assert due(weekly, now=NOW, local_hour=4, newest=NOW - timedelta(days=7))


def test_switched_off_never_runs() -> None:
    assert not due(plan(enabled=False), now=NOW, local_hour=4, newest=None)


def test_unset_values_fall_back_to_the_server_environment() -> None:
    server = get_settings()
    got = effective(shop_settings.defaults())
    assert got == Schedule(True, 24, server.backup_hour, server.backup_keep, server.backup_to_telegram)
    mine = effective({"backup_every_hours": 3, "backup_keep": 5, "backup_send_telegram": False})
    assert (mine.every_hours, mine.keep, mine.telegram) == (3, 5, False)


async def test_the_owner_sets_the_schedule_on_the_backups_page(http, env, session) -> None:  # noqa: F811
    owner = await login(http, "u_owner")
    body = {"enabled": True, "every_hours": 6, "hour": 3, "keep": 9, "telegram": False}
    resp = await owner.call("PUT", "/api/admin/backups/schedule", body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule"]["every_hours"] == 6
    listed = (await owner.call("GET", "/api/admin/backups")).json()["schedule"]
    assert {k: listed[k] for k in body} == body
    assert await backup_schedule.load(session) == Schedule(True, 6, 3, 9, False)
    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "backup.schedule"))
    assert audit is not None and audit.after["every_hours"] == 6

    odd = {**body, "every_hours": 36}
    resp = await owner.call("PUT", "/api/admin/backups/schedule", odd)
    assert resp.status_code == 422 and resp.json()["error"] == "backup_every_whole_days"
    resp = await owner.call("PUT", "/api/admin/backups/schedule", {**body, "every_hours": 0})
    assert resp.status_code == 422


async def test_the_schedule_is_not_editable_through_general_settings(http, env) -> None:  # noqa: F811
    admin = await login(http, "u_admin")  # settings.write, but not backups.manage
    resp = await admin.call("PUT", "/api/admin/settings", {"values": {"backup_every_hours": 1}})
    assert resp.status_code == 422 and resp.json()["error"] == "backup_settings_elsewhere"
    resp = await admin.call(
        "PUT",
        "/api/admin/backups/schedule",
        {"enabled": False, "every_hours": 1, "hour": 0, "keep": 1, "telegram": False},
    )
    assert resp.status_code == 403
    groups = (await admin.call("GET", "/api/admin/settings")).json()["groups"]
    assert "backups" not in groups


async def test_another_top_level_shop_cannot_change_the_server_schedule(http, env) -> None:  # noqa: F811
    other = await login(http, "other_owner")
    body = {"enabled": False, "every_hours": 1, "hour": 0, "keep": 1, "telegram": False}
    resp = await other.call("PUT", "/api/admin/backups/schedule", body)
    assert resp.status_code == 403


async def test_the_worker_follows_the_saved_schedule(env, session, monkeypatch) -> None:  # noqa: F811
    from paurubot.ops import backup, delivery
    from paurubot.ops.backup import BackupInfo
    from paurubot.worker import jobs

    await shop_settings.set_many(
        session, env.shop.id, {"backup_every_hours": 2, "backup_keep": 3, "backup_send_telegram": False}
    )
    await session.commit()
    made: list[str] = []
    pruned: list[int] = []
    sent: list[str] = []
    newest = [datetime.now(UTC) - timedelta(minutes=30)]

    async def create(*, label: str = "") -> BackupInfo:
        made.append(label)
        return BackupInfo("b.pbak", None, 1, datetime.now(UTC))  # type: ignore[arg-type]

    async def send(info: BackupInfo) -> int:
        sent.append(info.name)
        return 1

    def listing() -> list[BackupInfo]:
        return [BackupInfo("b.pbak", None, 1, newest[0])]  # type: ignore[arg-type]

    monkeypatch.setattr(backup, "create", create)
    monkeypatch.setattr(backup, "prune", lambda keep: pruned.append(keep) or [])
    monkeypatch.setattr(backup, "listing", listing)
    monkeypatch.setattr(delivery, "send_backup", send)
    monkeypatch.setattr(get_settings(), "backup_passphrase", __import__("pydantic").SecretStr("x" * 12))

    await jobs.daily_backup()
    assert made == []  # the last one is only half an hour old
    newest[0] = datetime.now(UTC) - timedelta(hours=2)
    await jobs.daily_backup()
    assert made == ["auto"] and pruned == [3] and sent == []

    await shop_settings.set_many(session, env.shop.id, {"backup_auto_enabled": False})
    await session.commit()
    await jobs.daily_backup()
    assert made == ["auto"]
