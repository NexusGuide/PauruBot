"""A child bot cannot copy private messages received by the main bot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.bot.routers import resale_broadcast
from paurubot.i18n import Texts


async def test_main_bot_cannot_open_child_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    found = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(id=2)))
    monkeypatch.setattr(resale_broadcast, "_running", found)
    capacity = AsyncMock()
    monkeypatch.setattr(resale_broadcast.resale_broadcasts, "remaining_today", capacity)
    call = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock())
    await resale_broadcast.broadcast_ask(call, None, SimpleNamespace(id=1), None, None, Texts("en"), state)  # type: ignore[arg-type]
    call.answer.assert_awaited_once_with(Texts("en")("rb_broadcast_own_bot"), show_alert=True)
    state.set_state.assert_not_awaited()
    capacity.assert_not_awaited()


async def test_stale_main_bot_form_cannot_queue_child_message(monkeypatch: pytest.MonkeyPatch) -> None:
    found = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(id=2)))
    monkeypatch.setattr(resale_broadcast, "_state_target", found)
    capacity = AsyncMock()
    monkeypatch.setattr(resale_broadcast.resale_broadcasts, "lock_capacity", capacity)
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    session = SimpleNamespace(add=AsyncMock())
    await resale_broadcast.broadcast_got(
        message, session, SimpleNamespace(id=1), None, None, Texts("en"), state
    )  # type: ignore[arg-type]
    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once_with(Texts("en")("rb_broadcast_own_bot"))
    capacity.assert_not_awaited()
    session.add.assert_not_called()
