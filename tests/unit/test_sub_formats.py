"""F-32: the customer can ask for the subscription in a named client format.

Before, the format was whatever the panel guessed from the app's User-Agent, so an app the panel
did not recognise got plain links it could not read. Panels that serve named formats now get them
through the shop's branded link, /s/<token>?format=<name>, and the Mini App and the subscription
page offer one link per format.
"""

from __future__ import annotations

import httpx

from paurubot.api.app import create_app
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.models import OrderKind
from paurubot.panels import registry
from paurubot.panels.base import SUB_FORMATS
from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.drivers.marzneshin import MarzneshinDriver
from paurubot.panels.drivers.xui import XuiDriver
from tests.unit import test_service_lifecycle
from tests.unit.test_service_lifecycle import _buy, _service

env = test_service_lifecycle.env


def test_each_panel_maps_our_format_names_to_its_own_paths() -> None:
    url = "https://panel.example.com/sub/abc"
    assert (
        MarzbanDriver.format_url(MarzbanDriver.__new__(MarzbanDriver), url, "xray-json")
        == f"{url}/v2ray-json"
    )
    assert (
        MarzbanDriver.format_url(MarzbanDriver.__new__(MarzbanDriver), url + "/", "clash") == f"{url}/clash"
    )
    assert (
        MarzneshinDriver.format_url(MarzneshinDriver.__new__(MarzneshinDriver), url + "?x=1", "xray-json")
        == f"{url}/xray?x=1"
    )
    assert XuiDriver.format_url(XuiDriver.__new__(XuiDriver), url, "clash") is None  # no named formats
    assert MarzbanDriver.format_url(MarzbanDriver.__new__(MarzbanDriver), None, "clash") is None
    assert registry.formats_of("marzban") == list(SUB_FORMATS)
    assert registry.formats_of("rebecca") == list(SUB_FORMATS)  # Marzban's paths
    assert (
        registry.formats_of("xui") == []
        and registry.formats_of("nope") == []
        and registry.formats_of(None) == []
    )


async def test_the_branded_link_serves_a_named_format(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    service = await _service(session, await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"]))
    await session.commit()
    fake = env["fake"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        clash = await client.get(f"/s/{service.sub_token}?format=clash", headers={"user-agent": "SomeApp/1"})
        assert clash.status_code == 200 and clash.text == f"format:clash:{service.panel_username}"
        assert fake.sub_formats[-1] == "clash"
        # A browser asking for a format gets the file, not the page.
        page_like = await client.get(
            f"/s/{service.sub_token}?format=sing-box",
            headers={"user-agent": "Mozilla/5.0", "accept": "text/html"},
        )
        assert page_like.text == f"format:sing-box:{service.panel_username}"
        assert fake.sub_formats[-1] == "sing-box"
        for bad in ("yaml", "../admin", "clash/../x", ""):
            response = await client.get(f"/s/{service.sub_token}", params={"format": bad})
            assert response.status_code == 404, bad
        # Without a format the panel still reads the app's User-Agent, as before.
        plain = await client.get(f"/s/{service.sub_token}", headers={"user-agent": "v2rayNG/1.9.1"})
        assert plain.status_code == 200 and fake.sub_formats[-1] is None
        page = await client.get(
            f"/s/{service.sub_token}", headers={"user-agent": "Mozilla/5.0", "accept": "text/html"}
        )
        assert 'id="fmt"' in page.text and f"/s/{service.sub_token}?format=clash-meta" in page.text


async def test_the_mini_app_offers_formats_only_on_the_branded_link(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.api.routes.webapp import service_view

    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    service = await _service(session, await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"]))
    cfg = await shop_settings.load(session, env["shop"].id)
    view = service_view(service, cfg)
    link = f"https://bot.example.com/s/{service.sub_token}"
    assert view["subscription_url"] == link
    assert view["sub_formats"] == [{"format": fmt, "url": f"{link}?format={fmt}"} for fmt in SUB_FORMATS]
    # With branded links off the customer sees the panel's own link, which we never rewrite.
    view = service_view(service, {**cfg, "branded_sub_links": False})
    assert view["subscription_url"] == service.subscription_url and view["sub_formats"] == []
