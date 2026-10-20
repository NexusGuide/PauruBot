from __future__ import annotations

from tests.unit import test_webapp_api as fixtures

app_env = fixtures.app_env
h = fixtures.h


async def test_http_order_replay_and_changed_body(app_env) -> None:  # type: ignore[no-untyped-def]
    client, plan = app_env["client"], app_env["plan"]
    body = {"kind": "purchase", "plan_id": plan.id, "intent_key": "http-order-123"}
    first = await client.post("/api/webapp/orders", headers=h(98101), json=body)
    assert first.status_code == 200
    second = await client.post("/api/webapp/orders", headers=h(98101), json=body)
    assert second.status_code == 200 and second.json()["order"]["id"] == first.json()["order"]["id"]
    changed = await client.post("/api/webapp/orders", headers=h(98101), json={**body, "label": "changed"})
    assert changed.status_code == 409 and changed.json()["error"] == "order_intent_conflict"
    other = await client.post("/api/webapp/orders", headers=h(98102), json=body)
    assert other.status_code == 200 and other.json()["order"]["id"] != first.json()["order"]["id"]


async def test_cancelled_draft_does_not_create_another_order(app_env) -> None:  # type: ignore[no-untyped-def]
    client, plan = app_env["client"], app_env["plan"]
    body = {"kind": "purchase", "plan_id": plan.id, "intent_key": "http-cancel-123"}
    first = await client.post("/api/webapp/orders", headers=h(98103), json=body)
    assert first.status_code == 200
    oid = first.json()["order"]["id"]
    assert (await client.post(f"/api/webapp/orders/{oid}/cancel", headers=h(98103))).status_code == 200
    again = await client.post("/api/webapp/orders", headers=h(98103), json=body)
    assert again.status_code == 409 and again.json()["error"] == "order_intent_closed"
