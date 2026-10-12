"""Renewal policy engine: what a renewal or add-on does to a service's time and data.

Every change that extends an existing service (renew, upgrade, extra volume, extra days) follows
one explicit policy with an independent rule for time and for data:

- ``append``: add to what is left (time from the current end date, data on top of the remainder);
- ``reset``: start again from now / from the purchased amount, discarding what is left;
- ``keep``: leave it as it is.

The absolute end state (target) is computed once, before the panel is touched, and stored on the
order so a retried job applies exactly the same values. After the panel call the bot reads the
account back and checks that the panel really holds the target (``verify``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from paurubot.db.models import OrderKind, Service, ServiceStatus
from paurubot.panels import PanelError, PanelUser

Rule = Literal["append", "reset", "keep"]
RULES: tuple[str, ...] = ("append", "reset", "keep")


@dataclass(frozen=True, slots=True)
class Policy:
    time: Rule
    volume: Rule
    reset_usage: bool

    def as_dict(self) -> dict[str, object]:
        return {"time": self.time, "volume": self.volume, "reset_usage": self.reset_usage}


# The four renewal modes a plan, panel or shop can choose (SVC-13), as explicit policies.
MODE_POLICIES: dict[str, Policy] = {
    "extend_reset": Policy("append", "reset", True),
    "extend_carry": Policy("append", "append", False),
    "restart_reset": Policy("reset", "reset", True),
    "restart_carry": Policy("reset", "append", True),
}
RENEW_MODES: tuple[str, ...] = tuple(MODE_POLICIES)
DEFAULT_MODE = "extend_reset"

# A lapsed service has nothing left worth carrying: it always starts a fresh period.
LAPSED = frozenset({ServiceStatus.EXPIRED, ServiceStatus.LIMITED, ServiceStatus.DISABLED})
FRESH = Policy("reset", "reset", True)

# Fixed policies of the other kinds of change.
KIND_POLICIES: dict[OrderKind, Policy] = {
    OrderKind.UPGRADE: Policy("reset", "reset", True),
    OrderKind.EXTRA_VOLUME: Policy("keep", "append", False),
    OrderKind.EXTRA_DAYS: Policy("append", "keep", False),
}
ADDONS = frozenset({OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS})


class UnsupportedChange(ValueError):
    """The order kind has no renewal policy (it does not change an existing service's limits)."""


def policy_for(
    kind: OrderKind, mode: str = DEFAULT_MODE, *, queued: bool = False, lapsed: bool = False
) -> Policy:
    """The policy a change of `kind` follows. `mode` only matters for renewals."""
    if kind == OrderKind.RENEW:
        if lapsed:
            return FRESH
        policy = MODE_POLICIES.get(mode, MODE_POLICIES[DEFAULT_MODE])
        if queued and policy.volume == "append":
            # A queued renewal starts when the period ends, with fresh data.
            policy = MODE_POLICIES[DEFAULT_MODE]
        return policy
    if kind in KIND_POLICIES:
        return KIND_POLICIES[kind]
    raise UnsupportedChange(kind.value)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the service had right before the change (kept for verification and audit)."""

    expire: datetime | None
    limit: int
    used: int
    status: str

    @classmethod
    def of(cls, service: Service) -> Snapshot:
        status = service.status.value if isinstance(service.status, ServiceStatus) else str(service.status)
        return cls(service.expire_at, int(service.data_limit or 0), int(service.used_bytes or 0), status)

    def as_dict(self) -> dict[str, object]:
        return {
            "expire": self.expire.isoformat() if self.expire else None,
            "limit": self.limit,
            "used": self.used,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class Target:
    """The absolute end state of a change: no deltas, so applying it twice changes nothing."""

    policy: Policy
    before: Snapshot
    set_expire: bool
    expire: datetime | None
    limit: int | None  # None: leave the data limit alone; 0: unlimited
    reset: bool

    def as_params(self) -> dict[str, object]:
        """The JSON shape stored in ``order.params["target"]``."""
        return {
            "set_expire": self.set_expire,
            "expire": self.expire.isoformat() if self.expire else None,
            "limit": self.limit,
            "reset": self.reset,
            "policy": self.policy.as_dict(),
            "before": self.before.as_dict(),
        }


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def compute(
    service: Service,
    policy: Policy,
    *,
    days: int,
    data_bytes: int,
    addon: bool = False,
    now: datetime | None = None,
) -> Target:
    """Apply `policy` to the service's current state.

    `days` / `data_bytes` are the amounts being bought (0 means unlimited for a plan). An add-on
    (`addon=True`) never turns an unlimited figure into a limited one.
    """
    now = now or _now()
    before = Snapshot.of(service)

    # Time.
    set_expire, expire = False, None
    if policy.time != "keep":
        if addon and before.expire is None:
            pass  # extra days on a service without an end date: nothing to extend
        elif days <= 0:
            set_expire = True  # an unlimited-time plan
        else:
            set_expire = True
            current = before.expire
            start = current if policy.time == "append" and current and current > now else now
            expire = start + timedelta(days=days)

    # Data.
    reset = policy.reset_usage
    limit: int | None = None
    if policy.volume != "keep":
        if not addon and data_bytes <= 0:
            limit = 0  # an unlimited-data plan; resetting usage only makes sense with a fresh volume
            reset = policy.volume == "reset"
        elif addon and before.limit == 0:
            limit = 0  # unlimited stays unlimited
            reset = False
        else:
            remaining = max(0, before.limit - before.used) if before.limit else 0
            allowance = data_bytes if policy.volume == "reset" else remaining + data_bytes
            limit = allowance if reset else before.used + allowance
    return Target(policy, before, set_expire, expire, limit, reset)


# --- verification -------------------------------------------------------------------------

# Panels store end dates differently (seconds, milliseconds, whole days): a renewal is confirmed
# when the panel's end date is within this slack of the target.
EXPIRE_SLACK = timedelta(hours=26)
LIMIT_SLACK = 16 * 1024**2  # panels that count data in (fractional) GB round a little
USAGE_CHECK_FLOOR = 128 * 1024**2  # below this the reset cannot be told apart from new traffic


class NotConfirmed(PanelError):
    """The panel does not show the change after it was applied. Retrying re-applies the target."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("renewal not confirmed by panel: " + ", ".join(problems))
        self.problems = problems


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def verify(target: dict[str, Any], info: PanelUser, *, check_enabled: bool = True) -> list[str]:
    """Problems found comparing the panel's account with a stored target; empty when confirmed."""
    problems: list[str] = []
    if check_enabled and info.status == "disabled":
        problems.append("still_disabled")
    if not info.limits_known:
        return problems  # the panel cannot report limits (PNL-11); ours are authoritative
    if target.get("set_expire"):
        wanted = _parse(target.get("expire"))
        actual = info.expire_at
        if wanted is None:
            if actual is not None:
                problems.append("expire_mismatch")
        elif actual is None or abs(actual - wanted) > EXPIRE_SLACK:
            problems.append("expire_mismatch")
    limit = target.get("limit")
    if limit is not None:
        wanted_limit, actual_limit = int(limit), int(info.data_limit or 0)
        if wanted_limit == 0:
            if actual_limit != 0:
                problems.append("limit_mismatch")
        elif abs(actual_limit - wanted_limit) > max(LIMIT_SLACK, wanted_limit // 1000):
            problems.append("limit_mismatch")
    before = target.get("before") or {}
    used_before = int(before.get("used") or 0)
    if target.get("reset") and used_before >= USAGE_CHECK_FLOOR and int(info.used_bytes) >= used_before:
        problems.append("usage_not_reset")
    return problems


def untouched(target: dict[str, Any]) -> dict[str, Any] | None:
    """The state the panel shows when a change was never applied, in the shape `verify` takes."""
    before = target.get("before")
    if not isinstance(before, dict):
        return None
    return {
        "set_expire": bool(target.get("set_expire")),
        "expire": before.get("expire"),
        "limit": before.get("limit") if target.get("limit") is not None else None,
        "reset": False,
    }


def should_verify(panel_options: dict[str, Any] | None, driver: object) -> bool:
    """Verification is on unless the panel (option) or its driver opts out."""
    if (panel_options or {}).get("verify_renewals") is False:
        return False
    return bool(getattr(driver, "verify_changes", True))
