"""F-04: capability probes report limits and always clean up the probe user."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelDriver,
    PanelError,
    PanelHealth,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
)
from paurubot.panels.capabilities import PROBE_PREFIX, report


class FakeDriver(PanelDriver):
    def __init__(self, *, fail_update: bool = False) -> None:
        self.users: dict[str, PanelUser] = {}
        self.fail_update = fail_update
        self.deleted: list[str] = []

    async def health_check(self) -> PanelHealth:
        return PanelHealth(True, "ok", 3, "1.0")

    async def list_options(self) -> list[PanelOption]:
        return [PanelOption("1", "default")]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        user = PanelUser(username=spec.username)
        self.users[spec.username] = user
        return user

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        return self.users.get(ref.username)

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        if self.fail_update:
            raise PanelError("forbidden", 403)
        return self.users[ref.username]

    async def reset_usage(self, ref: PanelRef) -> None:
        return None

    async def delete_user(self, ref: PanelRef) -> None:
        self.deleted.append(ref.username)
        self.users.pop(ref.username, None)

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        return self.users[ref.username]

    async def close(self) -> None:
        return None


def _by_key(result: dict[str, Any]) -> dict[str, str]:
    return {c["key"]: c["status"] for c in result["checks"]}


async def test_read_only_report_never_creates_users() -> None:
    driver = FakeDriver()
    result = await report(driver)
    checks = _by_key(result)
    assert checks["connect"] == "ok"
    assert checks["list_users"] == "warn"  # base driver: listing unsupported
    assert "create" not in checks
    assert driver.users == {}


async def test_write_probe_cleans_up_even_when_a_step_fails() -> None:
    driver = FakeDriver(fail_update=True)
    result = await report(driver, write=True)
    checks = _by_key(result)
    assert checks["create"] == "ok"
    assert checks["update"] == "fail"
    assert checks["delete"] == "ok"
    assert result["status"] == "fail"
    assert driver.users == {}
    assert driver.deleted and driver.deleted[0].startswith(PROBE_PREFIX)


async def test_unhealthy_panel_does_not_claim_writes_were_tested() -> None:
    class DownDriver(FakeDriver):
        async def health_check(self) -> PanelHealth:
            return PanelHealth(False, "offline", 0)

    driver = DownDriver()
    result = await report(driver, write=True)
    assert result["status"] == "fail"
    assert result["write_tested"] is False
    assert driver.users == {} and driver.deleted == []


async def test_cleanup_failure_reports_the_exact_probe_username() -> None:
    class DeleteFailDriver(FakeDriver):
        async def delete_user(self, ref: PanelRef) -> None:
            raise PanelError("forbidden", 403)

    driver = DeleteFailDriver()
    result = await report(driver, write=True)
    assert result["status"] == "fail"
    check = next(c for c in result["checks"] if c["key"] == "delete")
    username = next(iter(driver.users))
    assert username in check["detail"] and "by hand" in check["detail"]


async def test_create_failure_does_not_delete_an_unknown_user() -> None:
    class CreateFailDriver(FakeDriver):
        async def create_user(self, spec: UserSpec) -> PanelUser:
            raise PanelError("forbidden", 403)

    driver = CreateFailDriver()
    result = await report(driver, write=True)
    assert _by_key(result)["create"] == "fail"
    assert driver.deleted == []
