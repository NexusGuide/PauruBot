"""Renewal reservation state machine: where a change to an existing service stands.

A renewal, upgrade or add-on touches the panel, and a panel call can fail *after* the panel has
already applied it (lost response, timeout, worker crash). Refunding such an order would give the
change away; retrying it blindly could apply it twice. Every modification order therefore carries
an explicit state in ``order.params["renewal"]``:

- ``pending``: the target is computed and stored; the panel has not been touched yet
- ``running``: panel calls are in progress
- ``uncertain``: a panel call failed or the panel did not confirm it; it may or may not be applied
- ``applied``: the panel confirmed the target
- ``failed``: the panel was never changed; refunding is safe
- ``reconciled``: an uncertain change was later found applied on the panel

Only ``pending`` and ``failed`` orders may be refunded. An uncertain order whose job gave up is
*parked* and settled by reading the panel (``lifecycle.reconcile_renewal``), never by a blind retry.
"""

from __future__ import annotations

from typing import Any

from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind

PENDING, RUNNING, UNCERTAIN, APPLIED, FAILED, RECONCILED = (
    "pending",
    "running",
    "uncertain",
    "applied",
    "failed",
    "reconciled",
)
STATES = (PENDING, RUNNING, UNCERTAIN, APPLIED, FAILED, RECONCILED)

TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({PENDING}),
    PENDING: frozenset({RUNNING, FAILED}),
    RUNNING: frozenset({APPLIED, UNCERTAIN}),
    UNCERTAIN: frozenset({RUNNING, RECONCILED, FAILED}),
    APPLIED: frozenset(),
    FAILED: frozenset(),
    RECONCILED: frozenset(),
}
SETTLED = frozenset({APPLIED, RECONCILED})
REFUNDABLE = frozenset({None, PENDING, FAILED})

# Order kinds that change an existing service and so go through this state machine.
KINDS = frozenset({OrderKind.RENEW, OrderKind.UPGRADE, OrderKind.EXTRA_VOLUME, OrderKind.EXTRA_DAYS})

HISTORY_LIMIT = 20


class IllegalTransition(Exception):
    def __init__(self, current: str | None, wanted: str) -> None:
        super().__init__(f"renewal cannot go from {current} to {wanted}")
        self.current = current
        self.wanted = wanted


def record(order: Order) -> dict[str, Any]:
    value = (order.params or {}).get("renewal")
    return dict(value) if isinstance(value, dict) else {}


def state(order: Order) -> str | None:
    value = record(order).get("state")
    return str(value) if value in STATES else None


def is_settled(order: Order) -> bool:
    return state(order) in SETTLED


def refundable(order: Order) -> bool:
    """Whether refunding cannot give away a change the panel may hold."""
    from paurubot.provisioning import renewal_policy

    if order.kind not in KINDS:
        return True
    params = order.params or {}
    target = params.get("target") or {}
    if target and (state(order) is None or renewal_policy.reset_untracked(target, params)):
        return False  # an old stored target is not proof that the panel was never touched
    return state(order) in REFUNDABLE


def parked(order: Order) -> bool:
    return bool(record(order).get("parked"))


def _save(order: Order, data: dict[str, Any]) -> None:
    order.params = {**(order.params or {}), "renewal": data}


def move(order: Order, new: str, note: str | None = None) -> None:
    """Change state, refusing transitions the machine does not allow."""
    current = state(order)
    if new not in TRANSITIONS[current]:
        raise IllegalTransition(current, new)
    data = record(order)
    entry: dict[str, Any] = {"state": new, "at": utcnow().isoformat()}
    if note:
        entry["note"] = note[:200]
    history = [*list(data.get("history") or []), entry][-HISTORY_LIMIT:]
    data.update(state=new, history=history, parked=False)
    if new == RUNNING:
        data["attempts"] = int(data.get("attempts") or 0) + 1
    _save(order, data)


def park(order: Order, reason: str) -> None:
    """The job gave up on an order that may be applied: leave it for reconciliation."""
    data = record(order)
    data.update(parked=True, parked_reason=reason[:200], parked_at=utcnow().isoformat())
    _save(order, data)


def note_check(order: Order, outcome: str) -> int:
    """Count a reconciliation look at the panel; returns how many there have been."""
    data = record(order)
    checks = int(data.get("checks") or 0) + 1
    data.update(checks=checks, last_check=outcome[:200], last_check_at=utcnow().isoformat())
    _save(order, data)
    return checks
