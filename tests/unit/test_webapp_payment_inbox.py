from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from paurubot.db.base import utcnow

from paurubot.db.models import Payment
from paurubot.payments import engine
from tests.unit import test_webapp_api as fixtures

app_env = fixtures.app_env
h = fixtures.h


async def topup(client, uid, key):  # type: ignore[no-untyped-def]
    response = await client.post(
        "/api/webapp/wallet/topup",
        headers=h(uid),
        json={"method": "card", "amount": 90_000, "intent_key": key},
    )
    assert response.status_code == 200
    return response.json()["payment"]


async def test_owned_resume_uses_snapshot_and_never_calls_provider(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    first = await topup(client, 98201, "inbox-card-key-1")
    create = AsyncMock(side_effect=AssertionError("resume must not create an invoice"))
    verify = AsyncMock(side_effect=AssertionError("resume must not credit or verify"))
    monkeypatch.setattr(engine.provider("card"), "create", create)
    monkeypatch.setattr(engine.provider("card"), "verify", verify)
    listed = await client.get("/api/webapp/payments", headers=h(98201))
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert listed.json()["items"][0]["id"] == first["payment_id"]
    assert listed.json()["items"][0]["action"] == "resume"
    assert "meta" not in listed.json()["items"][0] and "provider_ref" not in listed.json()["items"][0]
    resumed = await client.get(f"/api/webapp/payments/{first['payment_id']}/resume", headers=h(98201))
    assert resumed.status_code == 200
    saved = resumed.json()["payment"]
    assert (saved["payment_id"], saved["amount"], saved["provider"]) == (
        first["payment_id"],
        first["amount"],
        first["provider"],
    )
    assert saved["accounts"][0]["number"] == first["accounts"][0]["number"]
    other = await client.get(f"/api/webapp/payments/{first['payment_id']}/resume", headers=h(98202))
    assert other.status_code == 404
    assert (await client.get("/api/webapp/payments", headers=h(98202))).json()["items"] == []
    payment = await session.get(Payment, first["payment_id"])
    payment.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    closed = await client.get(f"/api/webapp/payments/{first['payment_id']}/resume", headers=h(98201))
    assert closed.status_code == 409 and closed.json()["error"] == "payment_resume_unavailable"
    create.assert_not_awaited()
    verify.assert_not_awaited()


async def test_inbox_cursor_is_stable_and_excludes_other_customers(app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    ids = [(await topup(client, 98203, f"cursor-payment-{i}"))["payment_id"] for i in range(22)]
    await topup(client, 98204, "cursor-foreign-1")
    first = (await client.get("/api/webapp/payments", headers=h(98203))).json()
    assert [p["id"] for p in first["items"]] == list(reversed(ids))[:20]
    last = (await client.get(f"/api/webapp/payments?before={first['next_before']}", headers=h(98203))).json()
    assert [p["id"] for p in last["items"]] == list(reversed(ids))[-2:]
    assert last["next_before"] is None
