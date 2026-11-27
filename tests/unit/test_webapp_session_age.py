"""AUD-5: Mini App sign-in data is a bearer credential; it is accepted for two hours, not a day.

The signed initData string is the only thing the Mini App sends to prove who the customer is,
and it moves money (wallet transfer), deletes services and pays orders. It was accepted for 24
hours, so one leaked copy (a screenshot of the developer tools, a shared log) was a day of full
access. Telegram signs a fresh copy every time the app is opened from the bot.
"""

from __future__ import annotations

import json
from pathlib import Path

from paurubot.config import Settings
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import init_data

app_env = test_webapp_api.app_env
ROOT = Path(__file__).resolve().parents[2]


def test_the_default_lifetime_is_two_hours() -> None:
    assert Settings.model_fields["webapp_auth_max_age"].default == 2 * 3600


async def test_old_sign_in_data_is_refused(app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]

    async def me(age: int) -> int:
        headers = {"x-telegram-init-data": init_data(98001, age=age)}
        return (await client.get("/api/webapp/me", headers=headers)).status_code

    assert await me(60) == 200
    assert await me(2 * 3600 - 60) == 200
    assert await me(3 * 3600) == 401
    assert await me(20 * 3600) == 401


def test_an_expired_session_says_how_to_get_a_new_one() -> None:
    for locale in ("fa", "en"):
        copy = json.loads(
            (ROOT / "frontend" / "src" / "app" / "locales" / f"{locale}.json").read_text(encoding="utf-8")
        )
        assert copy.get("err_invalid_init_data"), locale
