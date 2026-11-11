"""Foxima parity in the web panel: the same broadcast audiences, section buttons and pinning as
the bot, and a reply button on staff messages. The existing options are unchanged."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.db.models import Broadcast, Job
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_broadcast_to_one_panels_buyers_with_a_section_button_and_a_pin(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    body = {
        "text": "New servers",
        "kind": "panel",
        "panel_id": env.panel.id,
        "buttons": [{"text": "Buy now", "action": "buy"}],
        "pin": True,
        "pin_hours": 24,
    }
    made = await owner.call("POST", "/api/admin/broadcasts", body)
    assert made.status_code == 200, made.text
    row = await session.get(Broadcast, made.json()["id"])
    assert row.audience["kind"] == "panel" and row.audience["panel_id"] == env.panel.id
    assert row.audience["pin"] is True and row.audience["pin_hours"] == 24
    assert row.buttons == [{"text": "Buy now", "action": "buy"}]
    size = await owner.call("GET", f"/api/admin/broadcasts/audience?kind=panel&panel_id={env.panel.id}")
    assert size.status_code == 200 and size.json()["reachable"] >= 1
    targeted = await owner.call(
        "GET", f"/api/admin/broadcasts/targeted/audience?kind=panel&panel_id={env.panel.id}"
    )
    assert targeted.json() == size.json()  # the screen's count path agrees
    foreign = await owner.call(
        "GET", f"/api/admin/broadcasts/targeted/audience?kind=panel&panel_id={env.other_panel.id}"
    )
    assert foreign.json()["reachable"] == 0
    # Another shop's panel, no panel, an unknown section, a bad pin: refused.
    for bad in (
        {**body, "panel_id": env.other_panel.id},
        {**body, "panel_id": None},
        {**body, "buttons": [{"text": "x", "action": "admin"}]},
        {**body, "buttons": [{"text": "x", "action": "buy", "url": "https://a.b"}]},
        {**body, "pin_hours": 5000},
    ):
        assert (await owner.call("POST", "/api/admin/broadcasts", bad)).status_code in {404, 422}


async def test_broadcast_to_people_idle_for_days(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    made = await owner.call(
        "POST", "/api/admin/broadcasts", {"text": "We miss you", "kind": "idle", "idle_days": 30}
    )
    assert made.status_code == 200, made.text
    row = await session.get(Broadcast, made.json()["id"])
    assert row.audience["kind"] == "idle" and row.audience["idle_days"] == 30
    for days in (None, 0, 400):
        refused = await owner.call(
            "POST", "/api/admin/broadcasts", {"text": "x", "kind": "idle", "idle_days": days}
        )
        assert refused.status_code == 422
    # The plain options still work as before.
    plain = await owner.call(
        "POST", "/api/admin/broadcasts", {"text": "Hi", "buttons": [{"text": "Site", "url": "https://a.b"}]}
    )
    assert plain.status_code == 200


async def test_a_staff_message_can_carry_a_reply_button(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    done = await owner.call(
        "POST",
        "/api/admin/customers/bulk",
        {"action": "message", "ids": [env.customer_id], "text": "Please check your config", "reply": True},
    )
    assert done.status_code == 200, done.text
    job = await session.scalar(select(Job).where(Job.kind == "staff_message").order_by(Job.id.desc()))
    assert job is not None and job.payload["reply"] is True
