from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from paurubot.bot.routers import resale_bot


async def test_pending_input_rechecks_reseller_bot_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SimpleNamespace(id=1)
    child = SimpleNamespace(id=2, bot_token_enc="encrypted")
    monkeypatch.setattr(resale_bot, "_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(resale_bot.resale, "shop_of", AsyncMock(return_value=child))
    monkeypatch.setattr(resale_bot, "_allowed", AsyncMock(return_value=False))
    state: Any = SimpleNamespace(clear=AsyncMock())
    missing: Any = None
    assert await resale_bot._state_target(missing, missing, missing, missing, missing, state) is None
    state.clear.assert_awaited_once()
