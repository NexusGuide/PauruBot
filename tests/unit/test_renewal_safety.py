"""Regression boundaries: expiry evidence and a durable, non-repeatable reset reservation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot.db.models import ServiceStatus
from paurubot.panels import GB, PanelUser
from paurubot.provisioning import lifecycle
from paurubot.provisioning import renewal_policy as rp

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _target(*, used: int = 10 * GB) -> dict:
    return {
        "set_expire": True,
        "expire": (NOW + timedelta(days=2)).isoformat(),
        "limit": 50 * GB,
        "reset": True,
        "before": {"expire": (NOW + timedelta(days=1)).isoformat(), "used": used},
    }


def _info(*, used: int = 0, known: bool = True) -> PanelUser:
    return PanelUser(
        "safety-user",
        used_bytes=used,
        data_limit=50 * GB,
        expire_at=NOW + timedelta(days=2),
        limits_known=known,
    )


@pytest.mark.parametrize("hours", [1, 6, 24, 48, 720, -1, -24])
def test_previous_expiry_never_confirms_a_real_change(hours: int) -> None:
    before = NOW + timedelta(days=10)
    target = {
        "set_expire": True,
        "expire": (before + timedelta(hours=hours)).isoformat(),
        "before": {"expire": before.isoformat()},
    }
    info = PanelUser("u", expire_at=before)
    assert "expire_mismatch" in rp.verify(target, info)
    info.expire_at = before + timedelta(hours=hours)
    assert rp.verify(target, info) == []


def test_short_change_has_non_overlapping_rounding_slack() -> None:
    target = _target()
    info = _info()
    info.expire_at -= timedelta(hours=11)
    assert rp.verify(target, info) == []
    info.expire_at -= timedelta(hours=2)
    assert "expire_mismatch" in rp.verify(target, info)


def test_legacy_target_without_snapshot_requires_precise_expiry() -> None:
    target = {"set_expire": True, "expire": NOW.isoformat()}
    info = PanelUser("u", expire_at=NOW - timedelta(days=1))
    assert "expire_mismatch" in rp.verify(target, info)
    info.expire_at = NOW + timedelta(minutes=4)
    assert rp.verify(target, info) == []


@pytest.mark.parametrize(
    ("before", "used", "known", "confirmed"),
    [
        (10 * GB, 0, True, True),
        (10 * GB, GB, True, True),
        (10 * GB, 10 * GB, True, False),
        (10 * GB, 11 * GB, True, False),
        (1, 0, True, False),
        (0, 0, True, False),
        (10 * GB, 0, False, False),
    ],
)
def test_pending_reset_requires_observable_evidence(before, used, known, confirmed) -> None:
    assert rp.reset_confirmed(_target(used=before), _info(used=used, known=known)) is confirmed


def _objects(*, used: int = 10 * GB, params: dict | None = None):
    target = _target(used=used)
    order = SimpleNamespace(id=1, params=params or {})
    service = SimpleNamespace(
        panel_username="safety-user",
        panel_data={},
        status=ServiceStatus.ACTIVE,
        used_bytes=used,
        data_limit=50 * GB,
        expire_at=NOW + timedelta(days=1),
    )
    snapshots = []

    async def commit() -> None:
        snapshots.append(dict(order.params))

    session = SimpleNamespace(refresh=AsyncMock(), commit=AsyncMock(side_effect=commit))
    driver = SimpleNamespace(
        reset_usage=AsyncMock(),
        update_user=AsyncMock(return_value=_info()),
        get_user=AsyncMock(return_value=_info()),
    )
    return session, order, service, driver, target, snapshots


async def _apply(objects, *, verifies: bool = False):
    session, order, service, driver, target, _ = objects
    return await lifecycle._apply_target(
        session,
        order,
        service,
        driver,
        target,
        datetime.fromisoformat(target["expire"]),
        verifies,
    )


async def test_reset_intent_is_committed_before_remote_call() -> None:
    objects = _objects()
    session, order, _, driver, _, snapshots = objects

    async def reset(ref) -> None:
        assert snapshots[-1].get("reset_attempted")
        assert not snapshots[-1].get("reset_done")

    driver.reset_usage.side_effect = reset
    await _apply(objects)
    session.refresh.assert_awaited_once_with(order, with_for_update=True)
    assert order.params["reset_done"] is True
    driver.reset_usage.assert_awaited_once()


async def test_commit_failure_cannot_start_a_reset() -> None:
    objects = _objects()
    objects[0].commit.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await _apply(objects)
    objects[3].reset_usage.assert_not_awaited()


@pytest.mark.parametrize("verifies", [False, True])
async def test_lost_reset_response_is_reconciled_without_second_reset(verifies: bool) -> None:
    objects = _objects()
    _, order, _, driver, _, _ = objects
    driver.reset_usage.side_effect = TimeoutError("response lost after reset")
    with pytest.raises(TimeoutError):
        await _apply(objects, verifies=verifies)
    assert rp.reset_pending(order.params)
    # The remote counter dropped. Retry may read that evidence, but must never reset again.
    driver.get_user.return_value = _info(used=200 * 1024**2)
    await _apply(objects, verifies=verifies)
    driver.reset_usage.assert_awaited_once()
    assert order.params["reset_done"] is True


class WorkerKilled(BaseException):
    """A process death bypasses Exception handlers."""


async def test_worker_death_after_reset_reservation_does_not_repeat_reset() -> None:
    objects = _objects()
    driver = objects[3]
    driver.reset_usage.side_effect = WorkerKilled
    with pytest.raises(WorkerKilled):
        await _apply(objects)
    await _apply(objects)
    driver.reset_usage.assert_awaited_once()


@pytest.mark.parametrize(("used", "known"), [(0, True), (1, True), (10 * GB, False)])
async def test_unprovable_pending_reset_stays_uncertain(used: int, known: bool) -> None:
    objects = _objects(used=used, params={"reset_attempted": NOW.isoformat()})
    driver = objects[3]
    driver.get_user.return_value = _info(known=known)
    for _ in range(2):
        with pytest.raises(rp.NotConfirmed, match="reset_uncertain"):
            await _apply(objects)
    driver.reset_usage.assert_not_awaited()
    driver.update_user.assert_not_awaited()
    assert not objects[1].params.get("reset_done")


async def test_unchanged_counter_does_not_authorize_another_reset() -> None:
    objects = _objects(params={"reset_attempted": NOW.isoformat()})
    objects[3].get_user.return_value = _info(used=10 * GB)
    with pytest.raises(rp.NotConfirmed, match="reset_uncertain"):
        await _apply(objects)
    objects[3].reset_usage.assert_not_awaited()


async def test_recorded_reset_is_never_repeated_after_usage_verification_error() -> None:
    objects = _objects(params={"reset_done": True, "verification": {"problems": ["usage_not_reset"]}})
    await _apply(objects)
    objects[3].reset_usage.assert_not_awaited()
