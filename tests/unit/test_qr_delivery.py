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


async def test_custom_qr_caption_substitutes_safely_and_supports_link() -> None:
    service = SimpleNamespace(id=1, shop_id=1, label="My <VPN>", panel_username="u1")
    cfg = _cfg(qr_caption="Service {name} ({username}) {unknown} {name.__class__}", qr_show_link=True)
    _photo, caption = await handlers._delivery_photo(FA, service, LINK, cfg)
    assert "My &lt;VPN&gt;" in caption
    assert "(u1)" in caption
    assert "{name.__class__}" in caption
    assert LINK in caption


async def test_custom_qr_background_composites_png(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import io

    from PIL import Image

    from paurubot.bot import views

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), "#1e293b").save(buf, format="PNG")
    bg_bytes = buf.getvalue()

    async def fake_fetch(url: str) -> bytes | None:
        return bg_bytes if url == "https://cdn.example.com/bg.png" else None

    monkeypatch.setattr(views, "fetch_qr_background", fake_fetch)
    service = SimpleNamespace(id=1, shop_id=1, label="s1", panel_username="s1")
    cfg = _cfg(qr_background_url="https://cdn.example.com/bg.png")
    built = await views.build_qr_photo(FA, service, LINK, cfg)
    assert built is not None
    out = Image.open(io.BytesIO(built[0]))
    assert out.size == (600, 600)
