"""REF-08: each shop gives its Mini App its own accent, logo and notice."""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest

from paurubot import kv
from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.core import settings as shop_settings
from paurubot.db.models import StaffRole
from paurubot.notifications import telegram as notify_telegram
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env
PHOTO = b"\xff\xd8\xff\xe0" + b"jpeg" * 100


async def test_staff_set_a_preset_accent_and_a_clean_notice(session) -> None:  # type: ignore[no-untyped-def]
    from tests import factories

    shop = await factories.shop(session)
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="brand_owner", password="a-very-strong-pass"
    )
    await session.commit()
    transport = httpx.ASGITransport(app=create_app(), client=("10.4.4.4", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        login = await client.post(
            "/api/auth/login", json={"username": "brand_owner", "password": "a-very-strong-pass"}
        )
        headers = {"x-csrf-token": login.json()["csrf_token"]}
        bad = await client.put(
            "/api/admin/settings", json={"values": {"miniapp_accent": "#ff0000"}}, headers=headers
        )
        assert bad.status_code == 422 and bad.json()["error"] == "miniapp_accent_invalid"
        notice = "  Open   today\n\nuntil 22:00 " + "x" * 400
        ok = await client.put(
            "/api/admin/settings",
            json={"values": {"miniapp_accent": "teal", "miniapp_notice": notice}},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    assert cfg["miniapp_accent"] == "teal"
    assert cfg["miniapp_notice"].startswith("Open today until 22:00 x") and len(cfg["miniapp_notice"]) == 300


async def test_the_mini_app_gets_the_brand_and_the_bot_photo(app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(9101))).json()
    assert me["shop"]["brand"] == {"accent": None, "logo": False, "notice": None}
    assert (await client.get("/api/webapp/brand/logo", headers=h(9101))).status_code == 404
    from paurubot.db.session import session_scope

    async with session_scope() as s:
        await shop_settings.set_many(
            s, shop.id, {"miniapp_accent": "rose", "miniapp_logo": True, "miniapp_notice": "Hi"}
        )
    await kv.get_redis().delete(f"brand-logo:{shop.id}")
    asked = []

    class Bot:
        async def get_me(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(id=42)

        async def get_user_profile_photos(self, user_id, limit):  # type: ignore[no-untyped-def]
            asked.append(user_id)
            sizes = [SimpleNamespace(file_id=f"p{w}", width=w) for w in (160, 320, 640)]
            return SimpleNamespace(photos=[sizes])

        async def download(self, file_id):  # type: ignore[no-untyped-def]
            assert file_id == "p320"  # the size the home screen needs, not the original
            return io.BytesIO(PHOTO)

    async def bot_for(_shop_id):  # type: ignore[no-untyped-def]
        return Bot()

    monkeypatch.setattr(notify_telegram, "bot_for", bot_for)
    me = (await client.get("/api/webapp/me", headers=h(9101))).json()
    assert me["shop"]["brand"] == {"accent": "rose", "logo": True, "notice": "Hi"}
    for _ in range(2):
        logo = await client.get("/api/webapp/brand/logo", headers=h(9101))
        assert (
            logo.status_code == 200 and logo.content == PHOTO and logo.headers["content-type"] == "image/jpeg"
        )
    assert asked == [42]  # cached: Telegram is asked once
    assert (await client.get("/api/webapp/brand/logo")).status_code == 401


@pytest.mark.parametrize("value", ["", "green", "purple", "orange", "rose", "teal"])
def test_every_preset_has_a_palette(value: str) -> None:
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "frontend/src/app/app.css").read_text()
    assert not value or css.count(f'[data-accent="{value}"]') == 3  # light, dark by system, dark by choice
