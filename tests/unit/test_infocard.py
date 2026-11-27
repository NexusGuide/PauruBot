"""Service info card image (BOT-06)."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image

from paurubot.bot import infocard
from paurubot.db.models import ServiceStatus
from paurubot.i18n import Texts

GB = 1024**3


def service(**over):  # type: ignore[no-untyped-def]
    fields = {
        "data_limit": 50 * GB,
        "used_bytes": int(41.3 * GB),
        "expire_at": datetime.now(UTC) + timedelta(days=12, hours=2),
        "status": ServiceStatus.ACTIVE,
        "panel": SimpleNamespace(
            location=SimpleNamespace(title="🇩🇪 Germany"), name="de1", national_mode=False
        ),
        "plan": SimpleNamespace(title="Monthly 50"),
        "label": "Phone 🔥",
        "panel_username": "tg_5501_1",
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def test_volume_is_short_and_localised() -> None:
    assert infocard.volume(int(8.7 * GB)) == "8.7 GB"
    assert infocard.volume(50 * GB) == "50 GB"
    assert infocard.volume(512 * 1024) == "512 KB"
    assert infocard.volume(int(1.25 * GB), Texts("fa")) == "1.25 " + Texts("fa")("infocard_unit_gb")
    assert infocard.volume(-5) == "0 B"


def test_card_contents_in_english() -> None:
    card = infocard.build(Texts("en"), service(), "https://sub.example/s/x", footer="@shop_bot")  # type: ignore[arg-type]
    en = Texts("en")
    assert not card.rtl
    assert card.title == "Phone" and card.subtitle == "tg_5501_1"
    assert card.share is not None and abs(card.share - 8.7 / 50) < 0.01
    assert card.gauge_value == "8.7 GB"
    rows = dict(card.rows)
    assert rows[en("infocard_days_left")] == en("infocard_days_value", days=13)
    assert rows[en("infocard_location")] == "Germany"
    assert rows[en("infocard_plan")] == "Monthly 50"
    assert card.link == "https://sub.example/s/x" and card.footer == "@shop_bot"


def test_card_uses_persian_digits_and_handles_unlimited() -> None:
    if not infocard.shaping():
        pytest.skip("Persian layout needs raqm")
    card = infocard.build(
        Texts("fa"),
        service(data_limit=0, expire_at=None, status=ServiceStatus.ON_HOLD, plan=None, label=None),  # type: ignore[arg-type]
        None,
    )
    fa = Texts("fa")
    assert card.rtl and card.share is None and card.subtitle == ""
    assert card.gauge_value == fa("infocard_unlimited")
    assert "\u06f4\u06f1\u066b\u06f3" in card.gauge_caption
    rows = dict(card.rows)
    assert rows[fa("infocard_days_left")] == fa("infocard_on_hold")
    assert fa("infocard_plan") not in rows


def test_persian_falls_back_to_english_without_shaping(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(infocard, "shaping", lambda: False)
    card = infocard.build(Texts("fa"), service(), None)  # type: ignore[arg-type]
    assert not card.rtl and card.gauge_value == "8.7 GB"


def test_qr_can_be_left_out() -> None:
    card = infocard.build(Texts("en"), service(), "https://sub.example/s/x", qr=False)  # type: ignore[arg-type]
    assert card.link is None


@pytest.mark.parametrize("theme", sorted(infocard.THEMES) + ["unknown"])
@pytest.mark.parametrize("locale", ["fa", "en"])
def test_render_draws_a_card_with_its_qr(theme: str, locale: str) -> None:
    card = infocard.build(Texts(locale), service(label="x" * 200), "https://sub.example/s/abc")  # type: ignore[arg-type]
    image = Image.open(io.BytesIO(infocard.render(card, theme)))
    assert image.size == (infocard.WIDTH, infocard.HEIGHT) and image.format == "PNG"
    # the QR sits on a white tile at the reading end: both pure white and dark modules appear there
    left = 60 if card.rtl else infocard.WIDTH - 320
    tile = image.convert("L").crop((left, 200, left + 250, 440))
    low, high = tile.getextrema()
    assert low < 60 and high > 240


def test_render_low_and_empty_data() -> None:
    for used in (49 * GB, 60 * GB):
        card = infocard.build(Texts("en"), service(used_bytes=used), None)  # type: ignore[arg-type]
        assert infocard.render(card).startswith(b"\x89PNG")


def test_theme_setting_is_checked() -> None:
    from fastapi import HTTPException

    from paurubot.api.routes.admin.system import _check_services

    _check_services({"info_card_theme": "paper"})
    _check_services({})
    with pytest.raises(HTTPException) as exc:
        _check_services({"info_card_theme": "neon"})
    assert exc.value.detail == "info_card_theme_invalid"
