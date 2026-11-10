"""Small, non-secret projection of the existing per-shop service warning settings."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
from typing import Any


def warn_remaining_percent(cfg: Mapping[str, Any]) -> int:
    return int(cfg["warn_remaining_percent"] or 0)


def warn_days(cfg: Mapping[str, Any]) -> int:
    return int(cfg["warn_days"] or 0)


def service_alert_policy(cfg: Mapping[str, Any]) -> dict[str, int]:
    """Allowlist only; retain the worker's existing integer/None semantics.

    Worker reads stay lazy within their original guards. Delivery preferences, queue
    state, deduplication and status transitions are separate from warning thresholds.
    """
    return {
        "warn_remaining_percent": warn_remaining_percent(cfg),
        "warn_days": warn_days(cfg),
    }


def quiet_until(now: datetime, cfg: Mapping[str, Any], tz: tzinfo) -> datetime | None:
    """When a customer alert due `now` may go out, if `now` falls in the shop's quiet hours.

    The hours are local (the server's zone); a window may run over midnight (22 → 7). Off unless
    both ends are set and differ."""
    start, end = cfg.get("alerts_quiet_start"), cfg.get("alerts_quiet_end")
    if start is None or end is None:
        return None
    start, end = int(start) % 24, int(end) % 24
    if start == end:
        return None
    local = now.astimezone(tz)
    inside = start <= local.hour < end if start < end else local.hour >= start or local.hour < end
    if not inside:
        return None
    release = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if release <= local:
        release += timedelta(days=1)
    return release
