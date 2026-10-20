"""Small, non-secret projection of the existing per-shop service warning settings."""

from __future__ import annotations

from collections.abc import Mapping
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
