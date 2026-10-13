"""F-22: destructive staff actions need a single-use, typed second step."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from paurubot import kv
from paurubot.api import confirmations
from paurubot.core import confirm


class _Pipe:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.ops: list[tuple[str, str]] = []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, name: str) -> None:
        self.ops.append(("get", name))

    def delete(self, name: str) -> None:
        self.ops.append(("delete", name))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, name in self.ops:
            out.append(self.store.get(name) if op == "get" else int(self.store.pop(name, None) is not None))
        return out


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, name: str, value: str, ex: int | None = None) -> bool:
        self.store[name] = value
        self.ttl[name] = int(ex or 0)
        return True

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self.store)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(kv, "get_redis", lambda: fake)
    return fake


async def test_typed_phrase_confirms_once(redis: FakeRedis) -> None:
    issued = await confirm.issue("staff:1", "service.delete", 7, "user_7")
    assert redis.ttl["confirm:staff:1:service.delete:7"] == confirm.TTL_SECONDS
    assert issued.token not in redis.store["confirm:staff:1:service.delete:7"]  # only a hash is kept
    assert await confirm.consume("staff:1", "service.delete", 7, issued.token, " @USER_7 ", phrase="user_7")
    # single use: the same token can't confirm a second time
    assert not await confirm.consume("staff:1", "service.delete", 7, issued.token, "user_7")


async def test_wrong_text_burns_the_challenge(redis: FakeRedis) -> None:
    issued = await confirm.issue("staff:1", "service.delete", 7, "user_7")
    assert not await confirm.consume("staff:1", "service.delete", 7, issued.token, "user_8")
    assert not await confirm.consume("staff:1", "service.delete", 7, issued.token, "user_7")


async def test_challenge_is_bound_to_staff_action_and_target(redis: FakeRedis) -> None:
    issued = await confirm.issue("staff:1", "service.delete", 7, "user_7")
    assert not await confirm.consume("staff:2", "service.delete", 7, issued.token, "user_7")
    assert not await confirm.consume("staff:1", "order.refund", 7, issued.token, "user_7")
    assert not await confirm.consume("staff:1", "service.delete", 8, issued.token, "user_7")
    assert not await confirm.consume("staff:1", "service.delete", 7, "forged", "user_7")
    assert not await confirm.consume("staff:1", "service.delete", 7, None, "user_7")


async def test_changed_target_voids_the_confirmation(redis: FakeRedis) -> None:
    issued = await confirm.issue("staff:1", "order.refund", 3, "250000")
    assert not await confirm.consume("staff:1", "order.refund", 3, issued.token, "250000", phrase="300000")


async def test_persian_digits_and_separators_are_accepted(redis: FakeRedis) -> None:
    issued = await confirm.issue("staff:1", "order.refund", 3, "1250000")
    assert await confirm.consume("staff:1", "order.refund", 3, issued.token, "۱٬۲۵۰٬۰۰۰")


async def test_bot_nonce_is_single_use(redis: FakeRedis) -> None:
    nonce = await confirm.issue_nonce("tg:5", "service.delete", 9)
    assert 0 < nonce < 2**31
    assert not await confirm.consume_nonce("tg:5", "service.delete", 9, nonce + 1)
    nonce = await confirm.issue_nonce("tg:5", "service.delete", 9)
    assert not await confirm.consume_nonce("tg:6", "service.delete", 9, nonce)
    assert await confirm.consume_nonce("tg:5", "service.delete", 9, nonce)
    assert not await confirm.consume_nonce("tg:5", "service.delete", 9, nonce)
    assert not await confirm.consume_nonce("tg:5", "service.delete", 9, 0)


def test_selection_digest_tracks_the_exact_selection() -> None:
    assert confirm.selection_digest("delete", [3, 1, 2]) == confirm.selection_digest("delete", [1, 2, 3, 3])
    assert confirm.selection_digest("delete", [1, 2]) != confirm.selection_digest("delete", [1, 2, 3])
    assert confirm.selection_digest("delete", [1]) != confirm.selection_digest("disable", [1])


async def test_guard_enforces_only_when_enabled(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"on": False}

    async def enforced(db: Any, shop_id: int) -> bool:
        return state["on"]

    monkeypatch.setattr(confirmations, "enforced", enforced)
    ctx: Any = SimpleNamespace(shop_id=1, staff=SimpleNamespace(id=4))
    db: Any = None
    await confirmations.guard(db, ctx, "service.delete", 7, "user_7", None)  # off: old panels keep working

    state["on"] = True
    with pytest.raises(HTTPException) as err:
        await confirmations.guard(db, ctx, "service.delete", 7, "user_7", None)
    assert err.value.status_code == 428 and err.value.detail == "confirmation_required"

    issued = await confirmations.challenge(ctx, "service.delete", 7, "user_7")
    assert issued["phrase"] == "user_7" and issued["action"] == "service.delete"
    fields = confirmations.ConfirmFields(confirm_token=issued["token"], confirm_text="user_7")
    await confirmations.guard(db, ctx, "service.delete", 7, "user_7", fields)
    with pytest.raises(HTTPException):
        await confirmations.guard(db, ctx, "service.delete", 7, "user_7", fields)  # replay
