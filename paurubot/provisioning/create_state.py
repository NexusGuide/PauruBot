"""Creation intent for new panel accounts: may the order be refunded? (SEC-F08)

A purchase or trial creates an account on a panel. The create call can fail *after* the panel
has created the account (lost response, timeout, worker crash, panel down right after). If the
job then runs out of retries, refunding would hand out a working account for free.

``order.params["create"]`` is committed **before** every create call:

- ``attempted``: a create request may have reached the panel; nothing proves the outcome
- ``absent``: reconciliation read the panel after the settle delay and the account is not there
- ``confirmed``: the account was created or adopted

Only an order that never attempted a create, or whose account is proven ``absent``, may be refunded.
A job that gives up on an ``attempted`` order *parks* it. ``lifecycle.reconcile_purchase``
settles it by reading the panel: if the account exists, provisioning resumes and adopts it; if
it is missing, the order is refunded. An unreachable panel or an ambiguous username leaves the
order with staff. A failed error code is never treated as proof, because one driver
``create_user`` can make several requests.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind

ATTEMPTED, ABSENT, CONFIRMED = "attempted", "absent", "confirmed"
STATES = (ATTEMPTED, ABSENT, CONFIRMED)
KINDS = frozenset({OrderKind.PURCHASE, OrderKind.TRIAL})
# Read the panel only once the last create request is surely finished on the panel side.
SETTLE_DELAY = timedelta(minutes=10)
MAX_RESUMES = 3
HISTORY_LIMIT = 20


def record(order: Order) -> dict[str, Any]:
    value = (order.params or {}).get("create")
    return dict(value) if isinstance(value, dict) else {}


def state(order: Order) -> str | None:
    value = record(order).get("state")
    return str(value) if value in STATES else None


def refundable(order: Order) -> bool:
    """Whether refunding cannot give away an account the panel may hold."""
    return order.kind not in KINDS or state(order) != ATTEMPTED


def parked(order: Order) -> bool:
    return bool(record(order).get("parked"))


def attempted_at(order: Order) -> datetime | None:
    raw = record(order).get("attempted_at")
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _save(order: Order, data: dict[str, Any], entry: str | None = None) -> None:
    if entry:
        history = [*list(data.get("history") or []), {"at": utcnow().isoformat(), "event": entry[:200]}]
        data["history"] = history[-HISTORY_LIMIT:]
    order.params = {**(order.params or {}), "create": data}


def attempt(order: Order, panel_id: int, username: str) -> None:
    """Record, before the create call, that the panel may hold the account from now on."""
    data = record(order)
    data.update(
        state=ATTEMPTED,
        panel_id=panel_id,
        username=username,
        attempted_at=utcnow().isoformat(),
        attempts=int(data.get("attempts") or 0) + 1,
        parked=False,
    )
    _save(order, data, "create requested")


def confirm(order: Order) -> None:
    data = record(order)
    if data:
        data.update(state=CONFIRMED, parked=False)
        _save(order, data, "account confirmed")


def absent(order: Order) -> None:
    data = record(order)
    data.update(state=ABSENT, parked=False)
    _save(order, data, "account not found on the panel")


def park(order: Order, reason: str) -> None:
    data = record(order)
    data.update(parked=True, parked_reason=reason[:200], parked_at=utcnow().isoformat())
    _save(order, data, "parked")


def unpark(order: Order) -> None:
    data = record(order)
    data["parked"] = False
    _save(order, data)


def note_check(order: Order, outcome: str) -> int:
    data = record(order)
    checks = int(data.get("checks") or 0) + 1
    data.update(checks=checks, last_check=outcome[:200], last_check_at=utcnow().isoformat())
    _save(order, data)
    return checks


def note_resume(order: Order) -> int:
    data = record(order)
    resumes = int(data.get("resumes") or 0) + 1
    data["resumes"] = resumes
    _save(order, data, "found on the panel; provisioning resumed")
    return resumes
