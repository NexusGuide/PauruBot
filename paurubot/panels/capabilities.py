"""Panel capability report (F-04): what this bot can really do on a panel with its credentials.

Read probes never change the panel. The write probe is opt-in: it creates one throw-away user
named `pbprobe_<hex>`, reads it back, changes its limit, revokes it when the panel supports that,
and always tries to delete it again, even when a step fails.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from paurubot.panels.base import GB, PanelDriver, PanelError, PanelRef, UserSpec

PROBE_PREFIX = "pbprobe_"


@dataclass(slots=True)
class Check:
    key: str
    status: str  # ok | warn | fail | skip
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _err(exc: Exception) -> str:
    return str(exc)[:200] or exc.__class__.__name__


async def _optional(key: str, probe: Any) -> Check:
    """A probe whose failure is a limitation of this panel kind, not a broken connection."""
    try:
        result = await probe()
    except PanelError as exc:
        return Check(key, "warn", _err(exc))
    return Check(key, "ok", result if isinstance(result, str) else "")


async def read_probes(driver: PanelDriver) -> list[Check]:
    checks: list[Check] = []
    try:
        health = await driver.health_check()
    except PanelError as exc:
        return [Check("connect", "fail", _err(exc))]
    checks.append(Check("connect", "ok" if health.ok else "fail", health.version or health.detail))
    if not health.ok:
        return checks

    async def options() -> str:
        return f"{len(await driver.list_options())} options"

    async def users() -> str:
        _, total = await driver.list_users(0, 1)
        return f"{total} users" if total is not None else ""

    async def admins() -> str:
        return f"{len(await driver.list_admins())} admins"

    checks.append(await _optional("list_options", options))
    checks.append(await _optional("list_users", users))
    checks.append(await _optional("list_admins", admins))
    checks.append(Check("client_ips", "ok" if driver.supports_ips else "skip"))
    checks.append(Check("webhooks", "ok" if driver.supports_webhooks else "skip"))
    checks.append(Check("delivery", "ok", driver.delivery))
    checks.append(Check("changes", "ok", ",".join(sorted(driver.changes))))
    return checks


async def write_probes(driver: PanelDriver) -> list[Check]:
    username = PROBE_PREFIX + secrets.token_hex(3)
    checks: list[Check] = []
    ref: PanelRef | None = None
    try:
        try:
            created = await driver.create_user(
                UserSpec(
                    username=username,
                    data_limit=GB,
                    expire_at=datetime.now(UTC) + timedelta(days=1),
                    note="PauruBot capability probe; safe to delete",
                )
            )
        except PanelError as exc:
            return [Check("create", "fail", _err(exc))]
        ref = PanelRef(created.username or username, dict(created.data or {}))
        checks.append(Check("create", "ok", ref.username))
        try:
            found = await driver.get_user(ref)
            checks.append(Check("read", "ok" if found else "fail", "" if found else "user not found"))
        except PanelError as exc:
            checks.append(Check("read", "fail", _err(exc)))
        try:
            await driver.update_user(ref, data_limit=2 * GB)
            checks.append(Check("update", "ok"))
        except PanelError as exc:
            checks.append(Check("update", "fail", _err(exc)))
        if "revoke" in driver.changes:
            try:
                await driver.revoke_subscription(ref)
                checks.append(Check("revoke", "ok"))
            except PanelError as exc:
                checks.append(Check("revoke", "warn", _err(exc)))
        else:
            checks.append(Check("revoke", "skip"))
    finally:
        if ref is not None:
            try:
                await driver.delete_user(ref)
                checks.append(Check("delete", "ok"))
            except PanelError as exc:
                checks.append(Check("delete", "fail", f"remove {ref.username} by hand: {_err(exc)}"))
    return checks


def summary(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "ok"


async def report(driver: PanelDriver, *, write: bool = False) -> dict[str, Any]:
    checks = await read_probes(driver)
    if write and checks and checks[0].status == "ok":
        checks += await write_probes(driver)
    return {"status": summary(checks), "write_tested": write, "checks": [c.as_dict() for c in checks]}
