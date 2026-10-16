"""Each auto-renew pass advances past busy, short-wallet and failed services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from paurubot.core import auto_renew
from paurubot.worker import jobs


@pytest.mark.parametrize("outcome", ["busy", "short", "cooldown", "placed"])
async def test_worker_processes_later_batches(monkeypatch, outcome: str) -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=6))

    @asynccontextmanager
    async def scope():
        yield session

    due = AsyncMock(side_effect=[[1, 2], [3, 4], [5, 6]])
    run = AsyncMock(return_value=outcome)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(auto_renew, "due", due)
    monkeypatch.setattr(auto_renew, "run_service", run)
    await jobs.auto_renew_services()
    assert run.await_args_list == [call(session, i) for i in range(1, 7)]
    assert [c.kwargs["after_id"] for c in due.await_args_list] == [0, 2, 4]
    assert all(c.kwargs["through_id"] == 6 for c in due.await_args_list)
    assert len({c.args[1] for c in due.await_args_list}) == 1  # one cutoff time per pass


async def test_worker_continues_after_one_service_fails(monkeypatch) -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=3))

    @asynccontextmanager
    async def scope():
        yield session

    run = AsyncMock(side_effect=[RuntimeError("panel unavailable"), "busy", "placed"])
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(auto_renew, "due", AsyncMock(side_effect=[[1, 2], [3]]))
    monkeypatch.setattr(auto_renew, "run_service", run)
    await jobs.auto_renew_services()
    assert run.await_args_list == [call(session, i) for i in range(1, 4)]


async def test_empty_database_does_not_scan(monkeypatch) -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    @asynccontextmanager
    async def scope():
        yield session

    due = AsyncMock()
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(auto_renew, "due", due)
    await jobs.auto_renew_services()
    due.assert_not_awaited()
