"""Locale catalogs, per-shop text overrides and safe message variables (BOT-01, BOT-04).

Every customer-visible string lives in `fa.json` / `en.json` (plus add-on catalogs named
`fa.<part>.json` / `en.<part>.json`). Shops can override any key per
locale from the admin panel; overrides go through the same restricted formatter, so a text edit
can use `{name}` style variables but can never reach object attributes or indexes.
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
import string
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import BotText

LOCALE_DIR = Path(__file__).parent
LOCALES = ("fa", "en")
DEFAULT_LOCALE = "fa"
_TTL = 30.0
_overrides: dict[int, tuple[float, dict[str, dict[str, str]]]] = {}


@lru_cache
def catalog(locale: str) -> dict[str, str]:
    """`{locale}.json` plus any `{locale}.<part>.json` add-on catalogs (e.g. `fa.hub.json`)."""
    path = LOCALE_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data: dict[str, str] = json.load(fh)
    for extra in sorted(LOCALE_DIR.glob(f"{locale}.*.json")):
        with extra.open(encoding="utf-8") as fh:
            for key, value in json.load(fh).items():
                data.setdefault(key, value)
    return data


def keys() -> list[str]:
    return sorted(catalog(DEFAULT_LOCALE))


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        if not field_name.isidentifier():
            raise KeyError(field_name)
        return self.get_value(field_name, args, kwargs), field_name

    def get_value(self, key: Any, args: Any, kwargs: Any) -> Any:
        if isinstance(key, int):
            raise KeyError(key)
        return kwargs[key]

    def format_field(self, value: Any, format_spec: str) -> str:
        if format_spec and format_spec not in {",", "d", ",d"}:
            format_spec = ""
        return super().format_field(value, format_spec)


_formatter = _SafeFormatter()


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def render(template: str, params: dict[str, Any]) -> str:
    try:
        return _formatter.vformat(template, (), params)
    except (KeyError, ValueError, IndexError, AttributeError):
        return template


TELEGRAM_HTML_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "a",
        "tg-spoiler",
        "blockquote",
        "tg-emoji",
    }
)


class _TelegramHtmlChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TELEGRAM_HTML_TAGS:
            self.problems.append(f"unsupported html tag <{tag}>")
        else:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in TELEGRAM_HTML_TAGS:
            self.problems.append(f"unsupported html tag </{tag}>")
        elif not self.stack or self.stack[-1] != tag:
            self.problems.append(f"mismatched closing tag </{tag}>")
        else:
            self.stack.pop()


def _validate_telegram_html(value: str) -> list[str]:
    parser = _TelegramHtmlChecker()
    parser.feed(value)
    parser.close()
    for tag in reversed(parser.stack):
        parser.problems.append(f"unclosed html tag <{tag}>")
    return parser.problems


def validate_override(key: str, value: str) -> list[str]:
    """Problems with an admin-edited text: blank value, unknown variables, broken braces or HTML."""
    problems: list[str] = []
    if not value or not value.strip():
        return ["text cannot be empty"]
    allowed = fields(catalog(DEFAULT_LOCALE).get(key, "")) | GLOBAL_VARIABLES
    try:
        used = fields(value)
    except ValueError:
        return ["unbalanced braces"]
    for name in sorted(used - allowed):
        problems.append(f"unknown variable {{{name}}}")
    if any(not name.isidentifier() for name in used):
        problems.append("variables must be plain names")
    problems.extend(_validate_telegram_html(value))
    if key.startswith("btn_") or "_btn_" in key:
        if "\n" in value or "\r" in value:
            problems.append("button label must be one line")
        if len(value.strip()) > 64:
            problems.append("button label is too long (max 64)")
    return problems


GLOBAL_VARIABLES = {"name", "first_name", "username", "shop", "support"}


async def load_overrides(session: AsyncSession, shop_id: int) -> dict[str, dict[str, str]]:
    cached = _overrides.get(shop_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    data: dict[str, dict[str, str]] = {}
    for row in await session.scalars(select(BotText).where(BotText.shop_id == shop_id)):
        data.setdefault(row.locale, {})[row.key] = row.value
    _overrides[shop_id] = (time.monotonic() + _TTL, data)
    return data


def invalidate(shop_id: int | None = None) -> None:
    if shop_id is None:
        _overrides.clear()
    else:
        _overrides.pop(shop_id, None)


class Texts:
    def __init__(
        self,
        locale: str,
        overrides: dict[str, dict[str, str]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.locale = locale if locale in LOCALES else DEFAULT_LOCALE
        self._overrides = overrides or {}
        self.context = dict(context or {})

    def with_locale(self, locale: str) -> Texts:
        return Texts(locale, self._overrides, self.context)

    def raw(self, key: str) -> str:
        return (
            self._overrides.get(self.locale, {}).get(key)
            or catalog(self.locale).get(key)
            or catalog(DEFAULT_LOCALE).get(key)
            or key
        )

    def __call__(self, key: str, **params: Any) -> str:
        return render(self.raw(key), {**self.context, **params})

    t = __call__

    def matches(self, text: str | None, key: str) -> bool:
        """Does a reply-keyboard press equal this button in any locale (or its override)?

        A renamed button still answers to its old label: customers keep the keyboard they were
        sent until the bot sends them a new one."""
        if not text:
            return False
        if text == self(key) or text in FORMER_LABELS.get(key, ()):
            return True
        for loc in LOCALES:
            override = self._overrides.get(loc, {}).get(key)
            if override and text == render(override, self.context):
                return True
            if text == render(catalog(loc).get(key, key), self.context):
                return True
        return False


# Labels a button had before it was renamed (fa, en), still matched on old keyboards.
FORMER_LABELS: dict[str, tuple[str, ...]] = {
    "btn_miniapp": ("\U0001f4f1 \u0627\u067e \u067e\u0627\u0626\u0648\u0631\u0648", "\U0001f4f1 Pauru app"),
}


async def texts_for(
    session: AsyncSession, shop_id: int, locale: str | None, context: dict[str, Any] | None = None
) -> Texts:
    return Texts(locale or DEFAULT_LOCALE, await load_overrides(session, shop_id), context)
