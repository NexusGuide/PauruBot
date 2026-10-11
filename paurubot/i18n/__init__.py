"""Locale catalogs, per-shop text overrides and safe message variables (BOT-01, BOT-04).

Every customer-visible string lives in `fa.json` / `en.json` (plus add-on catalogs named
`fa.<part>.json` / `en.<part>.json`). Shops can override any key per
locale from the admin panel; overrides go through the same restricted formatter, so a text edit
can use `{name}` style variables but can never reach object attributes or indexes.
"""

from __future__ import annotations

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


def validate_override(key: str, value: str) -> list[str]:
    """Problems with an admin-edited text: unknown variables or broken braces."""
    problems: list[str] = []
    allowed = fields(catalog(DEFAULT_LOCALE).get(key, "")) | GLOBAL_VARIABLES
    try:
        used = fields(value)
    except ValueError:
        return ["unbalanced braces"]
    for name in sorted(used - allowed):
        problems.append(f"unknown variable {{{name}}}")
    if any(not name.isidentifier() for name in used):
        problems.append("variables must be plain names")
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
        """Does a reply-keyboard press equal this button in any locale (or its override)?"""
        if not text:
            return False
        if text == self(key):
            return True
        return any(text == render(catalog(loc).get(key, key), self.context) for loc in LOCALES)


async def texts_for(
    session: AsyncSession, shop_id: int, locale: str | None, context: dict[str, Any] | None = None
) -> Texts:
    return Texts(locale or DEFAULT_LOCALE, await load_overrides(session, shop_id), context)
