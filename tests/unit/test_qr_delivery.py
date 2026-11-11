"""ClockBot parity: the QR after delivery can be turned off, and its caption can carry the link;
by default delivery is exactly as before."""

from __future__ import annotations

from types import SimpleNamespace

from paurubot.core import settings
from paurubot.i18n import Texts
from paurubot.notifications import handlers

FA = Texts("fa")
LINK = "https://sub.example.com/sub/abc123"


def _cfg(**values):  # type: ignore[no-untyped-def]
    cfg = settings.defaults()
    cfg.update(info_card_enabled=False, **values)
    return cfg


async def test_default_is_the_bare_qr_as_before() -> None:
    service = SimpleNamespace(id=1, shop_id=1, label="s1", panel_username="s1")
    photo, caption = await handlers._delivery_photo(FA, service, LINK, _cfg())
    assert caption == FA("qr_caption")


async def test_link_in_caption_and_qr_off() -> None:
    service = SimpleNamespace(id=1, shop_id=1, label="s1", panel_username="s1")
    _photo, caption = await handlers._delivery_photo(FA, service, LINK, _cfg(qr_show_link=True))
    assert caption.startswith(FA("qr_caption")) and LINK in caption
    assert await handlers._delivery_photo(FA, service, LINK, _cfg(qr_send_enabled=False)) is None
