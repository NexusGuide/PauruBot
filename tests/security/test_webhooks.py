"""Webhook authentication (SEC-11): secrets are required and compared in constant time."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from paurubot.api.app import create_app
from paurubot.api.routes.telegram import webhook_secret
from paurubot.config import get_settings
from paurubot.security.secretbox import encrypt
from tests import factories

pytestmark = pytest.mark.security


@pytest.fixture
async def client(session) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(), client=("10.4.4.4", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_telegram_webhook_needs_the_secret_header(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "bot_mode", "webhook")
    update = {"update_id": 1}
    assert (await client.post("/api/telegram/webhook", json=update)).status_code == 403
    wrong = {"x-telegram-bot-api-secret-token": "x" * 48}
    assert (await client.post("/api/telegram/webhook", json=update, headers=wrong)).status_code == 403
    assert webhook_secret() not in get_settings().bot_token.get_secret_value()


async def test_telegram_webhook_is_off_in_polling_mode(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "bot_mode", "polling")
    headers = {"x-telegram-bot-api-secret-token": webhook_secret()}
    assert (await client.post("/api/telegram/webhook", json={}, headers=headers)).status_code == 404


async def test_panel_webhook_rejects_missing_or_wrong_secret(client, session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    no_secret = await client.post(
        f"/api/webhooks/panel/{panel.id}", json={}, headers={"x-webhook-secret": "a"}
    )
    assert no_secret.status_code == 403
    panel.webhook_secret_enc = encrypt("webhook", "the-right-secret-value")
    await session.commit()
    wrong = await client.post(
        f"/api/webhooks/panel/{panel.id}", json={}, headers={"x-webhook-secret": "nope"}
    )
    assert wrong.status_code == 403
    missing = await client.post("/api/webhooks/panel/999999", json={}, headers={"x-webhook-secret": "x"})
    assert missing.status_code == 403
    right = await client.post(
        f"/api/webhooks/panel/{panel.id}", json=[], headers={"x-webhook-secret": "the-right-secret-value"}
    )
    assert right.status_code == 200 and right.json()["received"] == 0
