"""Audit trail of staff and system actions with before/after snapshots (ADM-03)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import AuditLog
from paurubot.logs import correlation_id

SECRET_FIELDS = {
    "password_hash",
    "password_enc",
    "bot_token_enc",
    "webhook_secret_enc",
    "tls_cert_pem",
    "key_hash",
    "payload_enc",
    "guest_token",
    "credentials_enc",
    "token_enc",
    "secret_enc",
}


def _scrub(value: Any) -> Any:
    """Recursively mask secret fields in plain dict/list data (JSON columns included)."""
    if isinstance(value, dict):
        return {
            key: "***" if key in SECRET_FIELDS and item is not None else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


@dataclass(slots=True, frozen=True)
class Actor:
    kind: str  # staff | customer | system
    id: int | None = None
    ip: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls("system")


def snapshot(obj: Any, fields: list[str] | None = None) -> dict[str, Any]:
    """Plain-column snapshot of an ORM object; secrets are masked."""
    mapper = inspect(obj).mapper
    data: dict[str, Any] = {}
    for column in mapper.column_attrs:
        key = column.key
        if fields is not None and key not in fields:
            continue
        value = getattr(obj, key)
        if key in SECRET_FIELDS:
            value = "<set>" if value else None
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        elif hasattr(value, "value") and not isinstance(value, (int, str)):
            value = value.value
        elif isinstance(value, (dict, list)):
            value = _scrub(value)
        data[key] = value
    return data


def diff(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if before is None or after is None:
        return before, after
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    return {k: before.get(k) for k in changed}, {k: after.get(k) for k in changed}


async def record(
    session: AsyncSession,
    *,
    shop_id: int | None,
    actor: Actor,
    action: str,
    entity: str | None = None,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    before, after = diff(before, after)
    row = AuditLog(
        shop_id=shop_id,
        actor_type=actor.kind,
        actor_id=actor.id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        ip=actor.ip,
        correlation_id=correlation_id.get(),
    )
    session.add(row)
    await session.flush()
    return row
