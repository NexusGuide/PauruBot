"""User-facing strings. All visible text lives in the locale JSON files, never in code."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from paurubot.services import settings as runtime_settings

LOCALE_DIR = Path(__file__).parent


@lru_cache
def _load(locale: str = "fa") -> dict[str, str]:
    with (LOCALE_DIR / f"{locale}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def keys() -> list[str]:
    return sorted(_load())


def default_text(key: str) -> str:
    return _load().get(key, key)


def t(key: str, **kwargs: Any) -> str:
    overrides = runtime_settings.get("texts") or {}
    template = overrides.get(key) or _load().get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return _load().get(key, key).format(**kwargs)
