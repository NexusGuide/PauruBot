"""F-22: the customer rates a support reply with a thumbs up or down, and staff see it.

Before, there was no way to tell from the desk whether an answer had helped short of reading
the rest of the thread.
"""

from __future__ import annotations

from paurubot.db.models import Ticket, TicketMessage, TicketStatus
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env


async def _thread(session, shop, customer_id: int):  # type: ignore[no-untyped-def]
    ticket = Ticket(shop_id=shop.id, customer_id=customer_id, subject="Slow", status=TicketStatus.ANSWERED)
    session.add(ticket)
    await session.flush()
    question = TicketMessage(ticket_id=ticket.id, author_type="customer", author_id=customer_id, text="Slow?")
    answer = TicketMessage(ticket_id=ticket.id, author_type="staff", text="Try the other server.")
    note = TicketMessage(ticket_id=ticket.id, author_type="staff", text="private", is_internal=True)
    session.add_all([question, answer, note])
    await session.commit()
    return ticket, question, answer, note


async def test_a_customer_rates_a_reply_and_can_take_it_back(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(97001))).json()
    ticket, question, answer, note = await _thread(session, shop, me["id"])
    path = f"/api/webapp/tickets/{ticket.id}/messages/{answer.id}/reaction"

    async def react(value, uid: int = 97001, target: str = path):  # type: ignore[no-untyped-def]
        return await client.put(target, headers=h(uid), json={"reaction": value})

    assert (await react("up")).json() == {"id": answer.id, "reaction": "up"}
    thread = (await client.get(f"/api/webapp/tickets/{ticket.id}", headers=h(97001))).json()
    by_id = {m["id"]: m for m in thread["messages"]}
    assert by_id[answer.id]["reaction"] == "up" and by_id[question.id]["reaction"] is None
    assert (await react("down")).json()["reaction"] == "down"
    assert (await react(None)).json()["reaction"] is None
    await session.refresh(answer)
    assert answer.reaction is None

    # Only a support reply on one's own ticket, never one's own message, a note, or a bad value.
    base = f"/api/webapp/tickets/{ticket.id}/messages"
    assert (await react("up", target=f"{base}/{question.id}/reaction")).status_code == 404
    assert (await react("up", target=f"{base}/{note.id}/reaction")).status_code == 404
    assert (await react("up", target=f"{base}/999999/reaction")).status_code == 404
    assert (await react("up", uid=97002)).status_code == 404  # someone else's ticket
    assert (await react("love")).status_code == 422
    assert (await client.put(path, headers=h(97001), json={"reaction": "up", "x": 1})).status_code == 422
    await session.refresh(note)
    assert note.reaction is None


async def test_staff_see_the_rating_in_the_desk(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.routes.admin import people

    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(97011))).json()
    ticket, _question, answer, _note = await _thread(session, shop, me["id"])
    await client.put(
        f"/api/webapp/tickets/{ticket.id}/messages/{answer.id}/reaction",
        headers=h(97011),
        json={"reaction": "down"},
    )
    assert "reaction" in people.MESSAGE_FIELDS
    await session.refresh(answer)
    assert people.plain(answer, people.MESSAGE_FIELDS)["reaction"] == "down"
