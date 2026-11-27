"""Small, non-secret projection of the existing per-shop service warning settings."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
from typing import Any

_GB = 1024**3


def warn_remaining_percent(cfg: Mapping[str, Any]) -> int:
    return int(cfg["warn_remaining_percent"] or 0)


def warn_remaining_gb(cfg: Mapping[str, Any]) -> int:
    return int(cfg.get("warn_remaining_gb") or 0)


def warn_days(cfg: Mapping[str, Any]) -> int:
    return int(cfg["warn_days"] or 0)


def warn_time_percent(cfg: Mapping[str, Any]) -> int:
    return int(cfg.get("warn_time_percent") or 0)


def is_low_volume(data_limit: int, used_bytes: int, warn_percent: int, *, warn_gb: int = 0) -> bool:
    """True when remaining data crosses either the percentage or fixed-GB threshold."""
    if data_limit <= 0:
        return False
    remaining = max(0, data_limit - used_bytes)
    if warn_percent > 0 and remaining * 100 <= warn_percent * data_limit:
        return True
    return warn_gb > 0 and remaining <= warn_gb * _GB


def service_period_seconds(service: Any) -> float | None:
    """Duration of the service's current period in seconds for percentage-based time warnings."""
    plan = getattr(service, "plan", None)
    days = int(getattr(plan, "days", 0) or 0) if plan is not None else 0
    if days > 0:
        return float(days * 86400)
    created = getattr(service, "created_at", None)
    expire = getattr(service, "expire_at", None)
    if isinstance(created, datetime) and isinstance(expire, datetime) and expire > created:
        return (expire - created).total_seconds()
    return None


def is_expiring(
    seconds: float,
    warn_days_value: int,
    *,
    total_seconds: float | None = None,
    warn_time_pct: int = 0,
) -> bool:
    """True when remaining time crosses either the fixed day threshold or the percentage threshold."""
    if seconds <= 0:
        return False
    if warn_days_value > 0 and seconds <= warn_days_value * 86400:
        return True
    if warn_time_pct > 0 and total_seconds is not None and total_seconds > 0:
        return seconds * 100 <= warn_time_pct * total_seconds
    return False


def service_alert_policy(cfg: Mapping[str, Any]) -> dict[str, int]:
    """Allowlist only; retain the worker's existing integer/None semantics.

    Worker reads stay lazy within their original guards. Delivery preferences, queue
    state, deduplication and status transitions are separate from warning thresholds.
    """
    return {
        "warn_remaining_percent": warn_remaining_percent(cfg),
        "warn_remaining_gb": warn_remaining_gb(cfg),
        "warn_days": warn_days(cfg),
        "warn_time_percent": warn_time_percent(cfg),
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
