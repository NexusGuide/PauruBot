"""Customer filters must run before pagination and remain tenant/owner scoped."""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from paurubot.db.models import Ticket, TicketDepartment, TicketStatus
from tests import factories
from tests.unit.test_webapp_api import app_env as app_env
from tests.unit.test_webapp_api import h

pytestmark = pytest.mark.security
UID = 81941


@pytest.fixture
async def history(session, app_env):  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(UID))).json()
    other_customer = await factories.customer(session, shop, telegram_id=81942)
    other_shop = await factories.shop(session)
    active = TicketDepartment(shop_id=shop.id, title="Technical")
    inactive = TicketDepartment(shop_id=shop.id, title="Retired desk", is_active=False)
    unused = TicketDepartment(shop_id=shop.id, title="Unused retired secret", is_active=False)
    foreign = TicketDepartment(shop_id=other_shop.id, title="Foreign routing", log_chat_id=-100123)
    session.add_all([active, inactive, unused, foreign])
    await session.flush()
    rows = []
    for i in range(47):
        row = Ticket(
            shop_id=shop.id,
            customer_id=me["id"],
            subject=f"Owned {i}",
            status=TicketStatus.CLOSED if i < 2 else TicketStatus.ANSWERED if i == 2 else TicketStatus.OPEN,
            department_id=inactive.id if i == 0 else None if i == 1 else active.id,
        )
        session.add(row)
        await session.flush()
        rows.append(row)
    hidden = [
        Ticket(
            shop_id=shop.id,
            customer_id=other_customer.id,
            subject="Hidden other customer",
            status=TicketStatus.CLOSED,
            department_id=active.id,
        ),
        Ticket(
            shop_id=other_shop.id,
            customer_id=me["id"],
            subject="Hidden inconsistent shop",
            status=TicketStatus.CLOSED,
            department_id=foreign.id,
        ),
    ]
    session.add_all(hidden)
    await session.commit()
    return {
        **app_env,
        "owner": me["id"],
        "active": active,
        "inactive": inactive,
        "unused": unused,
        "foreign": foreign,
        "rows": rows,
        "hidden": hidden,
    }


async def listing(env, **params):  # type: ignore[no-untyped-def]
    response = await env["client"].get("/api/webapp/tickets", headers=h(UID), params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_status_filter_reaches_closed_tickets_beyond_first_unfiltered_page(history) -> None:  # type: ignore[no-untyped-def]
    latest = await listing(history)
    assert len(latest["items"]) == 20 and all(t["status"] == "open" for t in latest["items"])
    filtered = await listing(history, status="closed")
    assert [t["id"] for t in filtered["items"]] == [t.id for t in reversed(history["rows"][:2])]
    assert filtered["next_before"] is None


@pytest.mark.parametrize("status", list(TicketStatus))
async def test_each_status_filters_before_limit(history, status) -> None:  # type: ignore[no-untyped-def]
    result = await listing(history, status=status.value)
    expected = [t.id for t in reversed(history["rows"]) if t.status == status]
    assert [t["id"] for t in result["items"]] == expected[:20]
    assert result["next_before"] == (expected[19] if len(expected) > 20 else None)


async def test_status_and_department_combine_and_general_means_null(history) -> None:  # type: ignore[no-untyped-def]
    active = await listing(history, status="closed", department_id=history["active"].id)
    assert active["items"] == [] and active["next_before"] is None
    retired = await listing(history, status="closed", department_id=history["inactive"].id)
    general = await listing(history, status="closed", department_id=0)
    assert [t["id"] for t in retired["items"]] == [history["rows"][0].id]
    assert [t["id"] for t in general["items"]] == [history["rows"][1].id]


async def test_cursor_walk_uses_same_server_filters_and_never_repeats(history) -> None:  # type: ignore[no-untyped-def]
    ids, before = [], None
    while True:
        params = {"status": "open", "department_id": history["active"].id}
        if before is not None:
            params["before"] = before
        page = await listing(history, **params)
        ids.extend(row["id"] for row in page["items"])
        before = page["next_before"]
        if before is None:
            break
    assert ids == [t.id for t in reversed(history["rows"][3:])]
    assert len(ids) == len(set(ids)) == 44


async def test_new_rows_and_deleted_anchor_do_not_offset_later_pages(session, history) -> None:  # type: ignore[no-untyped-def]
    first = await listing(history, status="open")
    anchor = first["next_before"]
    inserted = Ticket(
        shop_id=history["shop"].id, customer_id=history["owner"], subject="New", status=TicketStatus.OPEN
    )
    session.add(inserted)
    await session.execute(delete(Ticket).where(Ticket.id == anchor))
    await session.commit()
    second = await listing(history, status="open", before=anchor)
    assert [t["id"] for t in second["items"]] == [
        t.id for t in reversed(history["rows"]) if t.status == TicketStatus.OPEN and t.id < anchor
    ][:20]
    assert inserted.id not in [t["id"] for t in second["items"]]


async def test_history_metadata_keeps_active_creation_choices_and_only_owned_retired_desks(history) -> None:  # type: ignore[no-untyped-def]
    data = await listing(history, status="answered")
    assert data["departments"] == [{"id": history["active"].id, "title": "Technical"}]
    assert {d["id"] for d in data["filter_departments"]} == {history["active"].id, history["inactive"].id}
    assert all(set(d) == {"id", "title"} for d in data["filter_departments"])
    assert "Foreign routing" not in str(data) and "Unused retired secret" not in str(data)
    # Filter choices do not disappear when the current filtered page is empty.
    empty = await listing(history, department_id=0, status="answered")
    assert empty["items"] == [] and empty["filter_departments"] == data["filter_departments"]


async def test_foreign_unknown_and_inconsistent_department_links_do_not_authorize(session, history) -> None:  # type: ignore[no-untyped-def]
    bad_link = Ticket(
        shop_id=history["shop"].id,
        customer_id=history["owner"],
        subject="Bad department link",
        department_id=history["foreign"].id,
    )
    session.add(bad_link)
    await session.commit()
    foreign = await listing(history, department_id=history["foreign"].id)
    unknown = await listing(history, department_id=2**63 - 1)
    assert foreign == unknown and foreign["items"] == []
    assert history["foreign"].id not in {d["id"] for d in foreign["filter_departments"]}


async def test_foreign_cursor_never_grants_access(history) -> None:  # type: ignore[no-untyped-def]
    data = await listing(history, status="closed", before=history["hidden"][-1].id)
    assert [row["id"] for row in data["items"]] == [t.id for t in reversed(history["rows"][:2])]


@pytest.mark.parametrize(
    "params",
    [
        {"before": 0},
        {"before": -1},
        {"before": "abc"},
        {"before": 2**63},
        {"department_id": -1},
        {"department_id": "1.5"},
        {"department_id": "true"},
        {"department_id": 2**63},
        {"department_id": "0 OR 1=1"},
        {"status": "CLOSED"},
        {"status": "all"},
        {"status": ""},
        {"status": "closed' OR '1'='1"},
    ],
)
async def test_invalid_query_is_422(app_env, params) -> None:  # type: ignore[no-untyped-def]
    response = await app_env["client"].get("/api/webapp/tickets", headers=h(UID), params=params)
    assert response.status_code == 422


async def test_filters_still_require_authentication(app_env) -> None:  # type: ignore[no-untyped-def]
    response = await app_env["client"].get(
        "/api/webapp/tickets", params={"status": "closed", "department_id": 0}
    )
    assert response.status_code == 401


async def test_retired_filter_does_not_enable_new_ticket_creation(history) -> None:  # type: ignore[no-untyped-def]
    # Use a second customer so the normal open-ticket limit is not the rejection reason.
    response = await history["client"].post(
        "/api/webapp/tickets",
        headers=h(81945),
        json={"subject": "New", "text": "Help", "department_id": history["inactive"].id},
    )
    assert response.status_code == 400 and response.json()["error"] == "ticket_department_unknown"
