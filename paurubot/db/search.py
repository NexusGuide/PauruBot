"""Literal "contains" search for SQL LIKE (audit SEC-06).

`%` and `_` are LIKE wildcards: typed into a search box they must match themselves. Use as
`column.ilike(contains(text), escape=ESCAPE)`.
"""

from __future__ import annotations

ESCAPE = "\\"


def contains(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
