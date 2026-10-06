"""Read table rows from a MySQL/MariaDB dump or an SQLite file without loading either database.

The dump reader understands what mysqldump and phpMyAdmin write: CREATE TABLE blocks for column
order and (extended) INSERT statements, with MySQL string escapes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_CREATE = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?[`\"]?(\w+)[`\"]?\s*\(", re.IGNORECASE)
_COLUMN = re.compile(r"^\s*[`\"](\w+)[`\"]\s+\w+")
_INSERT = re.compile(
    r"^INSERT (?:IGNORE )?INTO [`\"]?(\w+)[`\"]?\s*(\(([^)]*)\))?\s*VALUES\s*", re.IGNORECASE
)
_ESCAPES = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}

SQLITE_MAGIC = b"SQLite format 3\x00"


class ReadError(ValueError):
    pass


def detect(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(16)
    if head == SQLITE_MAGIC:
        return "sqlite"
    return "mysql"


def _values(text: str, start: int) -> Iterator[list[Any]]:
    """Parse `(v, v, ...), (...)` from position `start` until the closing semicolon."""
    i, n = start, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == ";":
            return
        if text[i] != "(":
            raise ReadError(f"unexpected {text[i]!r} in INSERT values")
        i += 1
        row: list[Any] = []
        while True:
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i >= n:
                raise ReadError("unterminated row")
            ch = text[i]
            if ch == "'" or ch == '"':
                quote = ch
                i += 1
                parts: list[str] = []
                while i < n:
                    c = text[i]
                    if c == "\\" and i + 1 < n:
                        nxt = text[i + 1]
                        parts.append(_ESCAPES.get(nxt, nxt))
                        i += 2
                        continue
                    if c == quote:
                        if i + 1 < n and text[i + 1] == quote:
                            parts.append(quote)
                            i += 2
                            continue
                        i += 1
                        break
                    parts.append(c)
                    i += 1
                row.append("".join(parts))
            else:
                j = i
                while j < n and text[j] not in ",)":
                    j += 1
                token = text[i:j].strip()
                i = j
                if token.upper() == "NULL":
                    row.append(None)
                elif token.lower().startswith("_binary"):
                    row.append(token[7:].strip().strip("'"))
                elif token.lower().startswith("0x"):
                    try:
                        row.append(bytes.fromhex(token[2:]).decode("utf-8", "replace"))
                    except ValueError:
                        row.append(token)
                else:
                    try:
                        row.append(int(token))
                    except ValueError:
                        try:
                            row.append(float(token))
                        except ValueError:
                            row.append(token)
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == ")":
                i += 1
                break
            raise ReadError("malformed row")
        yield row


def mysql_rows(path: Path, tables: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (table, row) for the wanted tables of a MySQL dump, in file order."""
    columns: dict[str, list[str]] = {}
    current: str | None = None
    pending = ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if pending:
                pending += "\n" + line
                if not line.rstrip().endswith(";"):
                    continue
                statement, pending = pending, ""
            else:
                created = _CREATE.match(line)
                if created:
                    current = created.group(1).lower()
                    columns[current] = []
                    continue
                if current is not None:
                    if line.startswith(")"):
                        current = None
                        continue
                    column = _COLUMN.match(line)
                    if column:
                        columns[current].append(column.group(1))
                    continue
                if not line.upper().startswith(("INSERT INTO", "INSERT IGNORE")):
                    continue
                if not line.rstrip().endswith(";"):
                    pending = line
                    continue
                statement = line
            insert = _INSERT.match(statement)
            if not insert:
                continue
            table = insert.group(1).lower()
            if table not in tables:
                continue
            names = (
                [c.strip().strip('`"') for c in insert.group(3).split(",")]
                if insert.group(3)
                else columns.get(table, [])
            )
            for values in _values(statement, insert.end()):
                if names and len(names) == len(values):
                    yield table, dict(zip(names, values, strict=True))
                else:
                    yield table, {str(index): value for index, value in enumerate(values)}


def sqlite_rows(path: Path, tables: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        present = {
            row[0].lower(): row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for wanted in tables:
            real = present.get(wanted)
            if real is None:
                continue
            for row in conn.execute(f'SELECT * FROM "{real}"'):  # noqa: S608  name comes from sqlite_master
                yield wanted, dict(row)
    finally:
        conn.close()


def rows(path: Path, tables: set[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    wanted = {t.lower() for t in tables}
    if detect(path) == "sqlite":
        yield from sqlite_rows(path, wanted)
    else:
        yield from mysql_rows(path, wanted)
