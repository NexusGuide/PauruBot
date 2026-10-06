"""What other bots store, mapped onto PauruBot customers and services (MIG-01).

Each source reads its own tables and yields plain records; nothing here touches our database.
Column names are looked up loosely because these bots grew their schemas over many versions.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paurubot.migration.readers import rows


@dataclass(slots=True)
class OldCustomer:
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    phone: str | None = None
    balance: int = 0
    banned: bool = False
    reseller: bool = False


@dataclass(slots=True)
class OldService:
    telegram_id: int
    panel_key: str
    username: str
    plan: str | None = None
    status: str = "active"


@dataclass(slots=True)
class Extract:
    source: str
    customers: dict[int, OldCustomer] = field(default_factory=dict)
    services: list[OldService] = field(default_factory=list)
    panels: dict[str, str] = field(default_factory=dict)  # key -> human label (name, address)
    skipped: int = 0


def _get(row: dict[str, Any], *names: str) -> Any:
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int = 64) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("@")
    return text[:limit] or None


# MirzaBot and its forks (Faoxima): tables user, invoice, marzban_panel.
MIRZA_GONE = {
    "unpaid",
    "removebyuser",
    "remove",
    "removed",
    "reject",
    "pending",
    "waiting",
    "deleted",
    "delete",
}


def mirza(path: Path) -> Extract:
    out = Extract("mirza")
    for table, row in rows(path, {"user", "invoice", "marzban_panel"}):
        if table == "user":
            telegram_id = _int(_get(row, "id"))
            if not telegram_id or telegram_id <= 0:
                out.skipped += 1
                continue
            phone = _text(_get(row, "number"), 20)
            out.customers[telegram_id] = OldCustomer(
                telegram_id,
                username=_text(_get(row, "username")),
                first_name=_text(_get(row, "namecustom")) if _get(row, "namecustom") != "none" else None,
                phone=None if phone in (None, "none") else phone,
                balance=max(0, _int(_get(row, "Balance")) or 0),
                banned=str(_get(row, "User_Status") or "active").lower() in {"block", "blocked", "ban"},
                reseller=str(_get(row, "agent") or "f").lower() not in {"f", "0", "", "none"},
            )
        elif table == "invoice":
            telegram_id = _int(_get(row, "id_user"))
            username = _text(_get(row, "username"), 64)
            status = str(_get(row, "Status") or "").lower()
            if not telegram_id or not username or status in MIRZA_GONE:
                out.skipped += 1
                continue
            panel = str(_get(row, "Service_location") or "").strip()
            out.services.append(
                OldService(telegram_id, panel, username, _text(_get(row, "name_product"), 96), status)
            )
            out.panels.setdefault(panel, panel)
        elif table == "marzban_panel":
            name = str(_get(row, "name_panel") or "").strip()
            if name:
                out.panels[name] = f"{name} ({_get(row, 'url_panel') or '-'})"
    return out


# WizWiz (XUI time bot): tables users, orders_list, server_info.
WIZWIZ_ACTIVE = {"1", "true", "active"}


def wizwiz(path: Path) -> Extract:
    out = Extract("wizwiz")
    servers: dict[str, str] = {}
    for table, row in rows(path, {"users", "orders_list", "server_info"}):
        if table == "users":
            telegram_id = _int(_get(row, "userid", "user_id"))
            if not telegram_id:
                out.skipped += 1
                continue
            out.customers[telegram_id] = OldCustomer(
                telegram_id,
                username=_text(_get(row, "username")),
                first_name=_text(_get(row, "name")),
                phone=_text(_get(row, "phone"), 20),
                balance=max(0, _int(_get(row, "wallet")) or 0),
                reseller=str(_get(row, "is_agent") or "0") in {"1", "true"},
            )
        elif table == "orders_list":
            telegram_id = _int(_get(row, "userid", "user_id"))
            username = _text(_get(row, "remark"), 64)
            status = str(_get(row, "status") if _get(row, "status") is not None else "1").lower()
            if not telegram_id or not username or status not in WIZWIZ_ACTIVE:
                out.skipped += 1
                continue
            server = str(_get(row, "server_id") or "")
            out.services.append(OldService(telegram_id, server, username, None, "active"))
            out.panels.setdefault(server, f"server {server}")
        elif table == "server_info":
            server = str(_get(row, "id") or "")
            if server:
                servers[server] = str(_get(row, "title", "remark") or server)
    for key, title in servers.items():
        out.panels[key] = f"{title} (server {key})"
    return out


SOURCES = {"mirza": mirza, "faoxima": mirza, "wizwiz": wizwiz}


def guess(path: Path) -> str:
    """Pick the source by which tables the file has."""
    found = {table for table, _ in _first_rows(path)}
    if "invoice" in found or "marzban_panel" in found:
        return "mirza"
    if "orders_list" in found:
        return "wizwiz"
    raise ValueError("unknown_source")


def _first_rows(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    seen: set[str] = set()
    for table, row in rows(path, {"user", "invoice", "marzban_panel", "users", "orders_list"}):
        if table not in seen:
            seen.add(table)
            yield table, row
