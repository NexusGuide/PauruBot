"""Read-only projection of an existing checkout; never contact or create a provider.

Missing/uncertain/corrupt snapshots deliberately have no payment instructions.
The owning HTTP route must authenticate and scope the payment before using this.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from paurubot.db.models import Order, Payment


def _status(row: Any) -> str:
    return str(getattr(row.status, "value", row.status))


def safe_https(value: Any) -> str | None:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    if "\\" in value:
        return None
    try:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return None
        _ = url.port  # reject malformed ports and bracketed hosts
    except ValueError:
        return None
    return value


def _meta(payment: Payment) -> dict[str, Any]:
    return payment.meta if isinstance(payment.meta, dict) else {}


def uncertain(payment: Payment) -> bool:
    attempt = _meta(payment).get("hosted_attempt_v1")
    return isinstance(attempt, dict) and attempt.get("state") == "uncertain"


def saved_payment(payment: Payment, now: datetime, order: Order | None = None) -> dict[str, Any] | None:
    if (
        uncertain(payment)
        or _status(payment) != "pending"
        or (payment.expires_at and payment.expires_at <= now)
    ):
        return None
    if payment.order_id is not None and (
        order is None
        or order.id != payment.order_id
        or order.customer_id != payment.customer_id
        or order.shop_id != payment.shop_id
        or _status(order) != "pending"
        or (order.expires_at and order.expires_at <= now)
    ):
        return None
    snapshot = _meta(payment).get("checkout_intent_v1")
    if not isinstance(snapshot, dict):
        return None
    request, checkout = snapshot.get("request"), snapshot.get("checkout")
    if (
        not isinstance(request, dict)
        or request.get("provider") != payment.provider
        or not isinstance(checkout, dict)
    ):
        return None
    if checkout.get("provider_ref") != payment.provider_ref:
        return None
    result: dict[str, Any] = {
        "payment_id": payment.id,
        "provider": payment.provider,
        "amount": payment.amount,
    }
    instructions = checkout.get("instructions")
    if payment.provider == "card":
        accounts = instructions.get("accounts") if isinstance(instructions, dict) else None
        if not isinstance(accounts, list) or not accounts:
            return None
        if any(
            not isinstance(a, dict) or not isinstance(a.get("number"), str) or not a["number"]
            for a in accounts
        ):
            return None
        result["accounts"] = [
            {key: str(a.get(key) or "") for key in ("number", "holder", "bank")} for a in accounts
        ]
    elif payment.provider == "stars":
        # Creating a Telegram invoice link is external I/O, not a read-only resume.
        return None
    elif isinstance(instructions, dict) and instructions.get("network"):
        if any(
            not isinstance(instructions.get(k), str) or not instructions[k]
            for k in ("network", "label", "address", "amount")
        ):
            return None
        if any(
            instructions.get(k) is not None and not isinstance(instructions[k], str)
            for k in ("memo", "expires_at")
        ):
            return None
        result["crypto"] = {
            k: instructions.get(k) for k in ("network", "label", "address", "amount", "memo", "expires_at")
        }
    else:
        redirect = safe_https(checkout.get("redirect_url"))
        if redirect is None:
            return None
        result["redirect_url"] = redirect
    return result


def action(payment: Payment, now: datetime, order: Order | None = None) -> str:
    if uncertain(payment):
        return "review"
    return "resume" if saved_payment(payment, now, order) is not None else "status"
