"""F-14: who is waiting on whom, the priority cycle and canned reply placeholders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from paurubot.core import tickets
from paurubot.db.models import TicketStatus

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _ticket(**fields: Any) -> Any:
    base = {"id": 42, "status": TicketStatus.OPEN, "last_customer_at": NOW, "last_staff_at": None}
    return SimpleNamespace(**{**base, **fields})


def test_awaiting_staff() -> None:
    assert tickets.awaiting_staff(_ticket())
    assert tickets.awaiting_staff(_ticket(last_staff_at=NOW - timedelta(hours=1)))
    assert not tickets.awaiting_staff(_ticket(last_staff_at=NOW + timedelta(minutes=1)))
    assert not tickets.awaiting_staff(_ticket(status=TicketStatus.CLOSED))
    assert not tickets.awaiting_staff(_ticket(last_customer_at=None))
    assert tickets.waiting_since(_ticket()) == NOW


def test_priority_cycle() -> None:
    assert [tickets.next_priority(p) for p in tickets.PRIORITIES] == ["normal", "high", "urgent", "low"]
    assert tickets.next_priority("bogus") == "normal"
    assert set(tickets.PRIORITY_ICON) == set(tickets.PRIORITIES)


def test_canned_placeholders() -> None:
    customer = SimpleNamespace(display_name="Sara")
    text = tickets.render_canned("Hi {name}, about #{ticket} {other}", customer=customer, ticket=_ticket())
    assert text == "Hi Sara, about #42 {other}"
    assert tickets.render_canned("{name}!", customer=None, ticket=_ticket()) == "!"
