"""Signed form identity is scoped, short-lived and must match the host cookie."""

from __future__ import annotations

import pytest

from paurubot.security import guest_form


@pytest.fixture
def clock(monkeypatch):  # type: ignore[no-untyped-def]
    now = [1_800_000_000]
    monkeypatch.setattr(guest_form, "_now", lambda: now[0])
    return now


def test_random_signed_form_and_cookie_pair(clock):  # type: ignore[no-untyped-def]
    first, second = guest_form.issue(17), guest_form.issue(17)
    assert first != second and len(first) <= 160
    assert guest_form.valid(first, 17)
    assert guest_form.matches(first, first, 17)
    assert not guest_form.matches(first, second, 17)
    assert len(guest_form.digest(first)) == 64 and first not in guest_form.digest(first)


@pytest.mark.parametrize("age,expected", [(-1, False), (0, True), (1799, True), (1800, False), (3600, False)])
def test_expiry_and_future_issue_fail_closed(clock, age, expected):  # type: ignore[no-untyped-def]
    token = guest_form.issue(17)
    clock[0] += age
    assert guest_form.valid(token, 17) is expected


@pytest.mark.parametrize("bad", ["", "x" * 1000, "17.1.x.y", "سلام", "../buy/t/private", None])
def test_malformed_or_missing_tokens_and_cookies(clock, bad):  # type: ignore[no-untyped-def]
    token = guest_form.issue(17)
    assert not guest_form.valid(bad, 17)
    assert not guest_form.matches(token, bad, 17)
    assert not guest_form.matches(bad, token, 17)


def test_shop_time_and_signature_are_bound(clock):  # type: ignore[no-untyped-def]
    token = guest_form.issue(17)
    assert not guest_form.valid(token, 18)
    assert not guest_form.valid(token.replace("17.", "18.", 1), 18)
    assert not guest_form.valid(token.replace("1800000000", "1800000001"), 17)
    changed = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert not guest_form.valid(changed, 17)
