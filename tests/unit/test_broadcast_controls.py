"""F-16: broadcast schedule rules, stop checks and the blocked / deleted breakdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from paurubot.core import broadcasts
from paurubot.db.models import JobStatus

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _row(**fields: Any) -> Any:
    base = {"status": JobStatus.RUNNING, "run": 0, "total": 10, "sent": 4, "failed": 3, "blocked": 2}
    return SimpleNamespace(**{**base, "deleted": 1, **fields})


def test_schedule_must_be_future_aware_and_near() -> None:
    assert broadcasts.schedule_ok(None, NOW)
    assert broadcasts.schedule_ok(NOW + timedelta(hours=1), NOW)
    assert not broadcasts.schedule_ok(NOW - timedelta(minutes=1), NOW)
    assert not broadcasts.schedule_ok(NOW + timedelta(days=61), NOW)
    assert not broadcasts.schedule_ok(datetime(2026, 10, 14, 12, 0), NOW)  # no timezone: ambiguous


def test_runner_stops_when_paused_canceled_or_replaced() -> None:
    assert not broadcasts.should_stop(_row(), 0)
    assert not broadcasts.should_stop(_row(status=JobStatus.QUEUED), 0)
    for status in (JobStatus.PAUSED, JobStatus.CANCELED, JobStatus.DONE):
        assert broadcasts.should_stop(_row(status=status), 0), status
    # After a resume the old job (run 0) must not keep sending next to the new one (run 1).
    assert broadcasts.should_stop(_row(run=1), 0)
    assert not broadcasts.should_stop(_row(run=1), 1)


def test_undeliverable_reasons() -> None:
    assert broadcasts.undeliverable_reason("Forbidden: bot was blocked by the user") == "blocked"
    assert broadcasts.undeliverable_reason("Bad Request: user is deactivated") == "deleted"
    assert broadcasts.undeliverable_reason("Bad Request: chat not found") == "deleted"


def test_progress_counts_what_is_left() -> None:
    figures = broadcasts.progress(_row())
    assert figures["left"] == 3 and figures["blocked"] == 2 and figures["deleted"] == 1
    assert broadcasts.progress(_row(sent=9, failed=5))["left"] == 0
