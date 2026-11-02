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


async def test_telegram_is_answered_before_the_update_is_handled(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """AUD-4: a slow handler must not hold Telegram's request open.

    The update was handled inside the request. Past Telegram's timeout it sends the same update
    again, and handlers that already started work would run twice. Its body was also read whole,
    with no size limit, unlike every other webhook.
    """
    import asyncio

    from paurubot.api.routes import telegram as route

    monkeypatch.setattr(get_settings(), "bot_mode", "webhook")
    release, seen = asyncio.Event(), []

    class Slow:
        async def feed_update(self, bot, update, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(update.update_id)
            await release.wait()

    monkeypatch.setattr(route, "dispatcher", lambda: Slow())
    monkeypatch.setattr("paurubot.notifications.telegram.get_bot", lambda: None)
    headers = {"x-telegram-bot-api-secret-token": webhook_secret()}
    response = await asyncio.wait_for(
        client.post("/api/telegram/webhook", json={"update_id": 7}, headers=headers), timeout=2
    )
    assert response.status_code == 200
    await asyncio.sleep(0)
    assert seen == [7]  # handled, just not while Telegram waited
    release.set()
    await route.drain(grace=2)
    assert not route._handling

    huge = b'{"update_id": 8, "pad": "' + b"x" * (route.MAX_UPDATE + 1) + b'"}'
    response = await client.post(
        "/api/telegram/webhook", content=huge, headers={**headers, "content-type": "application/json"}
    )
    assert response.status_code == 413 and seen == [7]
