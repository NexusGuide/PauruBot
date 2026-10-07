"""Service info card: one shareable image with usage, days left, end date and the QR (BOT-06).

The card is drawn at twice its final size and scaled down, which smooths the ring and the rounded
corners without an antialiasing pass per shape. Persian text needs Pillow's raqm layout (bundled
in the wheels, with fribidi from the system); without it the card is offered only in English.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import qrcode
from PIL import Image, ImageDraw, ImageFont, features

from paurubot.core.formatting import days_left, jdate, local
from paurubot.db.models import Service, ServiceStatus
from paurubot.i18n import Texts

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
WIDTH, HEIGHT = 1200, 675
SCALE = 2
LOW_SHARE = 0.15


@dataclass(frozen=True, slots=True)
class Theme:
    page: str
    card: str
    text: str
    muted: str
    accent: str
    track: str
    low: str


THEMES: dict[str, Theme] = {
    "midnight": Theme("#0D1726", "#15233A", "#EEF3FA", "#8EA3C0", "#4FC3A1", "#22364F", "#FF8A3D"),
    "paper": Theme("#E6ECF1", "#FFFFFF", "#17212B", "#66768A", "#2F6FEB", "#E2E8EF", "#E8590C"),
    "rose": Theme("#24121B", "#361C29", "#FBEFF4", "#C99BB0", "#FF7AA8", "#4C283A", "#FFB547"),
    "amber": Theme("#1B170F", "#2A2318", "#FFF6E5", "#BFA982", "#F5B83D", "#3D3323", "#FF6B5C"),
}
DEFAULT_THEME = "midnight"

STATUS_COLORS: dict[ServiceStatus, str] = {
    ServiceStatus.ACTIVE: "#3DD68C",
    ServiceStatus.ON_HOLD: "#F5B83D",
    ServiceStatus.LIMITED: "#FF8A3D",
    ServiceStatus.EXPIRED: "#FF5C6C",
    ServiceStatus.DISABLED: "#9AA5B1",
    ServiceStatus.PENDING: "#6CB4FF",
    ServiceStatus.DELETED: "#9AA5B1",
}

_ARABIC = re.compile("[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
_FA_DIGITS = str.maketrans("0123456789.", "".join(chr(0x06F0 + d) for d in range(10)) + "\u066b")
# Emoji, flags and joiners have no glyph in the card font; they would draw as empty boxes.
_NO_GLYPH = re.compile("[\U00010000-\U0010ffff☀-➿️‍⃣]")


def shaping() -> bool:
    return bool(features.check("raqm"))


def card_locale(t: Texts) -> str:
    """Persian needs text shaping; fall back to English rather than drawing broken letters."""
    return t.locale if t.locale != "fa" or shaping() else "en"


@dataclass(frozen=True, slots=True)
class Card:
    rtl: bool
    title: str
    subtitle: str
    status: str
    status_color: str
    share: float | None  # remaining share of data, None when unlimited
    gauge_value: str
    gauge_caption: str
    rows: tuple[tuple[str, str], ...]
    link: str | None
    qr_caption: str
    footer: str


_UNITS = ("B", "KB", "MB", "GB", "TB")


def volume(num_bytes: int, t: Texts | None = None) -> str:
    """Bytes as a short amount with at most two decimals and no trailing zeros."""
    value = float(max(0, num_bytes))
    unit = _UNITS[0]
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            break
        value /= 1024
    text = f"{value:.2f}".rstrip("0").rstrip(".") if unit not in ("B", "KB") else f"{value:.0f}"
    return f"{text} {t(f'infocard_unit_{unit.lower()}') if t is not None else unit}"


def _clean(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _NO_GLYPH.sub("", text)).strip()


def build(t: Texts, service: Service, link: str | None, *, qr: bool = True, footer: str = "") -> Card:
    """Everything the card shows, in the customer's language, separate from the drawing."""
    t = t.with_locale(card_locale(t))
    fa = t.locale == "fa"

    def num(text: object) -> str:
        return str(text).translate(_FA_DIGITS) if fa else str(text)

    def amount(value: int) -> str:
        return num(volume(value, t))

    limit, used = service.data_limit, service.used_bytes
    if limit:
        remaining = max(0, limit - used)
        share: float | None = remaining / limit
        gauge_value = amount(remaining)
        gauge_caption = t("infocard_remaining")
        usage = t("infocard_used_of", used=amount(used), total=amount(limit))
    else:
        share = None
        gauge_value = t("infocard_unlimited")
        gauge_caption = t("infocard_used", used=amount(used))
        usage = amount(used)

    if service.expire_at is None:
        days, end = t("infocard_unlimited"), "-"
    else:
        left = days_left(service.expire_at) or 0
        days = t("infocard_days_value", days=num(left)) if left else t("infocard_ended")
        end = num(jdate(service.expire_at) if fa else local(service.expire_at).strftime("%Y-%m-%d"))
    if service.status == ServiceStatus.ON_HOLD and service.expire_at is None:
        days = t("infocard_on_hold")

    panel = service.panel
    location = panel.location.title if panel and panel.location else (panel.name if panel else "-")
    rows = [
        (t("infocard_days_left"), days),
        (t("infocard_ends"), end),
        (t("infocard_usage"), usage),
        (t("infocard_location"), _clean(location) or "-"),
    ]
    if service.plan is not None:
        rows.append((t("infocard_plan"), _clean(service.plan.title)))

    title = _clean(service.label or service.panel_username) or service.panel_username
    return Card(
        rtl=fa,
        title=title,
        subtitle=service.panel_username if title != service.panel_username else "",
        status=_clean(t(f"status_{service.status.value}")),
        status_color=STATUS_COLORS.get(service.status, "#9AA5B1"),
        share=share,
        gauge_value=gauge_value,
        gauge_caption=gauge_caption,
        rows=tuple(rows),
        link=link if qr else None,
        qr_caption=t("infocard_scan"),
        footer=_clean(footer),
    )


# --- drawing --------------------------------------------------------------------


@lru_cache(maxsize=32)
def _font(weight: str, px: int) -> ImageFont.FreeTypeFont:
    path = FONT_DIR / f"Vazirmatn-{weight}.ttf"
    engine = ImageFont.Layout.RAQM if shaping() else ImageFont.Layout.BASIC
    return ImageFont.truetype(str(path), px * SCALE, layout_engine=engine)


class _Canvas:
    def __init__(self, card: Card, theme: Theme) -> None:
        self.card, self.theme = card, theme
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), theme.page)
        self.draw = ImageDraw.Draw(self.image)
        self.rtl_text = card.rtl and shaping()

    def direction(self, text: str) -> str | None:
        """Persian lines that start with a Latin word still read right to left; Latin-only stays put."""
        if not self.rtl_text:
            return None
        return "rtl" if _ARABIC.search(text) else "ltr"

    def x(self, value: float, width: float = 0) -> float:
        """Left edge for an element placed `value` from the reading start."""
        return (WIDTH - value - width) if self.card.rtl else value

    def box(self, x: float, y: float, w: float, h: float, radius: float, fill: str) -> None:
        left = self.x(x, w)
        s = SCALE
        self.draw.rounded_rectangle((left * s, y * s, (left + w) * s, (y + h) * s), radius * s, fill=fill)

    def width(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        return float(self.draw.textlength(text, font=font, direction=self.direction(text))) / SCALE

    def fit(self, text: str, font: ImageFont.FreeTypeFont, room: float) -> str:
        if self.width(text, font) <= room:
            return text
        while text and self.width(text + "…", font) > room:
            text = text[:-1]
        return text.rstrip() + "…"

    def text(self, x: float, y: float, text: str, font: ImageFont.FreeTypeFont, fill: str) -> float:
        """Draw at `x` from the reading start with its top at `y`; returns the drawn width."""
        width = self.width(text, font)
        self.draw.text(
            (self.x(x, width) * SCALE, y * SCALE), text, font=font, fill=fill, direction=self.direction(text)
        )
        return width

    def centred(self, cx: float, y: float, text: str, font: ImageFont.FreeTypeFont, fill: str) -> None:
        left = self.x(cx) - self.width(text, font) / 2
        self.draw.text((left * SCALE, y * SCALE), text, font=font, fill=fill, direction=self.direction(text))


def _qr(link: str, px: int) -> Image.Image:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    code.add_data(link)
    code.make(fit=True)
    img = code.make_image(fill_color="#111827", back_color="#FFFFFF").get_image().convert("RGB")
    return img.resize((px, px), Image.Resampling.NEAREST)


def render(card: Card, theme_name: str = DEFAULT_THEME) -> bytes:
    theme = THEMES.get(theme_name) or THEMES[DEFAULT_THEME]
    c = _Canvas(card, theme)
    margin, pad = 28, 56
    c.box(margin, margin, WIDTH - 2 * margin, HEIGHT - 2 * margin, 40, theme.card)

    qr_size = 236 if card.link else 0
    content_end = WIDTH - pad - (qr_size + 48 if qr_size else 0)

    # status chip at the reading end of the header, title before it
    chip_font = _font("Bold", 22)
    chip_w = c.width(card.status, chip_font) + 62
    chip_x = WIDTH - pad - chip_w
    c.box(chip_x, pad + 4, chip_w, 46, 23, theme.track)
    dot = c.x(chip_x + 30)
    c.draw.ellipse(
        ((dot - 8) * SCALE, (pad + 19) * SCALE, (dot + 8) * SCALE, (pad + 35) * SCALE), fill=card.status_color
    )
    c.text(chip_x + 46, pad + 10, card.status, chip_font, theme.text)

    title_font = _font("Black", 44)
    c.text(pad, pad - 6, c.fit(card.title, title_font, chip_x - pad - 24), title_font, theme.text)
    if card.subtitle:
        sub_font = _font("Regular", 22)
        c.text(pad, pad + 58, c.fit(card.subtitle, sub_font, chip_x - pad - 24), sub_font, theme.muted)

    # data ring
    ring, stroke, top = 270, 24, 196
    cx, cy = pad + ring / 2, top + ring / 2
    left = c.x(cx) - ring / 2
    bounds = (left * SCALE, top * SCALE, (left + ring) * SCALE, (top + ring) * SCALE)
    c.draw.ellipse(bounds, outline=theme.track, width=stroke * SCALE)
    share = 1.0 if card.share is None else max(0.0, min(1.0, card.share))
    if share > 0:
        color = theme.low if card.share is not None and share < LOW_SHARE else theme.accent
        sweep = 360 * share
        start, end = (-90.0, -90 + sweep) if not card.rtl else (-90 - sweep, -90.0)
        if share >= 1:
            c.draw.ellipse(bounds, outline=color, width=stroke * SCALE)
        else:
            c.draw.arc(bounds, start, end, fill=color, width=stroke * SCALE)
    value_font = _font("Black", 36 if len(card.gauge_value) <= 9 else 30)
    c.centred(cx, cy - 36, card.gauge_value, value_font, theme.text)
    caption_font = _font("Regular", 20)
    c.centred(cx, cy + 14, c.fit(card.gauge_caption, caption_font, ring - 60), caption_font, theme.muted)

    # facts beside the ring
    label_font, value_font = _font("Regular", 21), _font("Bold", 27)
    col = pad + ring + 52
    room = content_end - col
    step = 74 if len(card.rows) <= 4 else 70
    y = 180 if len(card.rows) > 4 else 196
    for label, value in card.rows:
        c.text(col, y, c.fit(label, label_font, room), label_font, theme.muted)
        c.text(col, y + 26, c.fit(value, value_font, room), value_font, theme.text)
        y += step

    # QR on a white tile so every scanner reads it, whatever the theme
    if card.link:
        tile = qr_size + 28
        tile_x = WIDTH - pad - tile
        tile_y = 186
        c.box(tile_x, tile_y, tile, tile, 22, "#FFFFFF")
        code = _qr(card.link, qr_size * SCALE)
        c.image.paste(code, (int(c.x(tile_x + 14, qr_size) * SCALE), int((tile_y + 14) * SCALE)))
        cap_font = _font("Regular", 20)
        c.centred(
            tile_x + tile / 2,
            tile_y + tile + 14,
            c.fit(card.qr_caption, cap_font, tile),
            cap_font,
            theme.muted,
        )

    if card.footer:
        foot_font = _font("Bold", 22)
        c.text(pad, HEIGHT - pad - 34, c.fit(card.footer, foot_font, WIDTH - 2 * pad), foot_font, theme.muted)

    out = c.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def footer_for(bot: Any, t: Texts) -> str:
    """The bot's handle, so a shared card says where the service came from."""
    try:
        me = await bot.me() if bot is not None else None
    except Exception:  # the card is still useful without it
        me = None
    if me is not None and getattr(me, "username", None):
        return f"@{me.username}"
    return str(t.context.get("shop") or "")


async def make(t: Texts, service: Service, link: str | None, cfg: dict[str, Any], bot: Any = None) -> bytes:
    """Build and draw the card off the event loop; raises OSError or ValueError if drawing fails."""
    card = build(t, service, link, qr=bool(cfg["info_card_qr"]), footer=await footer_for(bot, t))
    return await asyncio.to_thread(render, card, str(cfg["info_card_theme"]))
