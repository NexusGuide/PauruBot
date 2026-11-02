"""SEC-F10: on-chain scans must see every confirmed transfer in the window, not one page.

TronGrid and Toncenter return newest transfers first, a page at a time. A busy (or dust-spammed)
address can push a customer's real payment past the first page; it must still be credited, the
work per scan must stay bounded, and an interrupted backfill must resume where it stopped.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import httpx

from paurubot import kv
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import PaymentStatus
from paurubot.payments import engine, watch
from paurubot.payments.chains import USDT_TRC20, ChainError, Client
from tests import factories

TRON = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"
TON = "EQ" + "a" * 46


def _trc20(tx: str, value: int, ms: int) -> dict[str, Any]:
    return {
        "transaction_id": tx,
        "to": TRON,
        "type": "Transfer",
        "value": str(value),
        "block_timestamp": ms,
        "token_info": {"address": USDT_TRC20, "decimals": 6},
    }


class FakeTronGrid:
    """Newest-first TronGrid with fingerprint paging and min/max_timestamp filters."""

    def __init__(self, items: list[dict[str, Any]], page_size: int = 200) -> None:
        self.items = items
        self.page_size = page_size
        self.requests: list[dict[str, str]] = []
        self.fail_on: set[int] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if len(self.requests) in self.fail_on:
            return httpx.Response(429, json={"Error": "rate limited"})
        assert params["only_confirmed"] == "true" and params["only_to"] == "true"
        assert params["order_by"] == "block_timestamp,desc"
        lo = int(params["min_timestamp"])
        hi = int(params.get("max_timestamp", 2**62))
        rows = sorted(
            (i for i in self.items if lo <= i["block_timestamp"] <= hi),
            key=lambda i: (-i["block_timestamp"], i["transaction_id"]),
        )
        start = int(params.get("fingerprint", "0"))
        page = rows[start : start + self.page_size]
        meta: dict[str, Any] = {"page_size": len(page)}
        if start + self.page_size < len(rows):
            meta["fingerprint"] = str(start + self.page_size)
        return httpx.Response(200, json={"data": page, "success": True, "meta": meta})


async def _shop(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "crypto_enabled": True,
            "crypto_networks": ["usdt_trc20"],
            "tron_address": TRON,
            "fx_source": "manual",
            "usdt_rate": 100_000,
        },
    )
    return shop


def _dust(count: int, newest_ms: int, step_ms: int = 1, prefix: str = "dust") -> list[dict[str, Any]]:
    return [_trc20(f"{prefix}{i:06d}", 1, newest_ms - i * step_ms) for i in range(count)]


async def _payment(session, cust, amount: int, age: timedelta = timedelta(0)):  # type: ignore[no-untyped-def]
    started = await engine.start(session, cust, "usdt_trc20", amount)
    started.payment.created_at = utcnow() - age
    await session.commit()
    return started.payment


async def _scan_until_paid(session, client, grid, payment, runs: int) -> int:  # type: ignore[no-untyped-def]
    """Scan repeatedly; every run must stay inside the page budget. Returns the paying run."""
    for run in range(1, runs + 1):
        before = len(grid.requests)
        await watch.scan(session, client)
        assert len(grid.requests) - before <= watch.MAX_PAGES
        await session.refresh(payment)
        if payment.status == PaymentStatus.PAID:
            return run
    raise AssertionError(f"payment {payment.id} not credited in {runs} scans")


async def test_payment_behind_a_full_page_of_dust_is_credited(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "usdt_trc20", 200_000)
    await session.commit()
    now_ms = int(time.time() * 1000)
    real = _trc20("r" * 64, started.payment.provider_amount or 0, now_ms - 5_000)
    # 450 newer spam transfers push the real payment onto the third page.
    grid = FakeTronGrid([*_dust(450, now_ms), real])
    client = Client(httpx.MockTransport(grid), pacing={})

    assert await watch.scan(session, client) == 1
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.PAID
    assert started.payment.provider_tx_id == "r" * 64
    assert len(grid.requests) == 3
    # Following pages repeat the original query and only add the continuation.
    assert "fingerprint" not in grid.requests[0]
    assert [r.get("fingerprint") for r in grid.requests[1:]] == ["200", "400"]


async def test_backfill_is_bounded_and_resumes_across_runs(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(watch, "MAX_PAGES", 2)
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    payment = await _payment(session, cust, 200_000, timedelta(minutes=40))
    await _payment(session, cust, 900_000, timedelta(minutes=40))  # stays open: keeps scanning
    now_ms = int(time.time() * 1000)
    # 700 transfers two seconds apart; the real one is older than all of them.
    grid = FakeTronGrid(
        [*_dust(700, now_ms, 2_000), _trc20("r" * 64, payment.provider_amount or 0, now_ms - 1_500_000)]
    )
    client = Client(httpx.MockTransport(grid), pacing={})

    # Run 1 spends its whole budget from the newest end and records where it stopped.
    assert await watch.scan(session, client) == 0
    assert len(grid.requests) == 2
    # Later runs read the short live edge, then continue the gap with a stored restart point.
    assert await _scan_until_paid(session, client, grid, payment, runs=6) >= 2
    resumed = [r for r in grid.requests[2:] if "max_timestamp" in r]
    assert resumed and int(resumed[0]["max_timestamp"]) == now_ms - 399 * 2_000
    assert payment.provider_tx_id == "r" * 64

    # Once the window is caught up, a scan reads only the live edge: one request, not the window.
    for _ in range(6):
        before = len(grid.requests)
        await watch.scan(session, client)
        if len(grid.requests) - before == 1:
            break
    else:
        raise AssertionError("scan never settled on the live edge")
    assert int(grid.requests[-1]["min_timestamp"]) >= now_ms - (watch.RESCAN + 1) * 1000


async def test_new_transfers_above_a_pending_gap_are_still_read_first(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(watch, "MAX_PAGES", 2)
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    old = await _payment(session, cust, 200_000, timedelta(minutes=50))
    new = await _payment(session, cust, 300_000, timedelta(minutes=1))
    now_ms = int(time.time() * 1000)
    grid = FakeTronGrid(
        [*_dust(900, now_ms - 60_000, 2_000), _trc20("o" * 64, old.provider_amount or 0, now_ms - 2_000_000)]
    )
    client = Client(httpx.MockTransport(grid), pacing={})
    assert await watch.scan(session, client) == 0  # two pages of dust, gap recorded
    # The new payment's transfer lands above everything already read and is credited next run,
    # while the older range is still being backfilled.
    grid.items.append(_trc20("n" * 64, new.provider_amount or 0, now_ms))
    assert await watch.scan(session, client) == 1
    await session.refresh(new)
    await session.refresh(old)
    assert new.status == PaymentStatus.PAID and old.status == PaymentStatus.PENDING
    assert await _scan_until_paid(session, client, grid, old, runs=8)
    assert old.provider_tx_id == "o" * 64


async def test_failure_mid_sweep_keeps_what_was_read_and_resumes(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    payment = await _payment(session, cust, 200_000, timedelta(minutes=30))
    now_ms = int(time.time() * 1000)
    real = _trc20("r" * 64, payment.provider_amount or 0, now_ms - 900_000)
    grid = FakeTronGrid([*_dust(250, now_ms, 2_000), real])
    grid.fail_on = {2}  # the second page is rate limited
    client = Client(httpx.MockTransport(grid), pacing={})
    assert await watch.scan(session, client) == 0
    assert len(grid.requests) == 2
    assert await watch.scan(session, client) == 1
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID
    # The retry continued from page one's oldest row rather than re-reading the window.
    assert any(r.get("max_timestamp") == str(now_ms - 199 * 2_000) for r in grid.requests[2:])


async def test_an_ambiguous_transfer_goes_to_staff_and_credits_one_payment(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SEC-F18: a transfer two payments could claim waits for a person, visibly, in the review
    queue (it used to sit in meta, unseen), and its id lets only one of them be approved."""
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    first = await engine.start(session, cust, "usdt_trc20", 200_000)
    second = await engine.start(session, cust, "usdt_trc20", 200_000)
    second.payment.provider_amount = first.payment.provider_amount  # forced collision
    await session.commit()
    now_ms = int(time.time() * 1000)
    transfer = _trc20("a" * 64, first.payment.provider_amount or 0, now_ms - 1_000)
    grid = FakeTronGrid([transfer])
    client = Client(httpx.MockTransport(grid), pacing={})
    assert await watch.scan(session, client) == 0  # two candidates: nothing credited by itself
    for started in (first, second):
        await session.refresh(started.payment)
        assert engine.awaiting_review(started.payment)
        assert started.payment.meta["late_tx"] == "a" * 64
    assert await engine.approve_manual(session, first.payment.id, staff_id=None)
    await session.commit()
    assert not await engine.approve_manual(
        session, second.payment.id, staff_id=None
    )  # one transfer, one credit
    await session.refresh(first.payment)
    assert first.payment.status == PaymentStatus.PAID and first.payment.provider_tx_id == "a" * 64


async def test_corrupt_or_missing_scan_state_falls_back_to_the_full_window(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "usdt_trc20", 200_000)
    await session.commit()
    now_ms = int(time.time() * 1000)
    await kv.get_redis().set(watch.state_key(shop.id, "usdt_trc20", TRON), json.dumps({"hw": "x", "gaps": 5}))
    grid = FakeTronGrid([_trc20("r" * 64, started.payment.provider_amount or 0, now_ms)])
    assert await watch.scan(session, Client(httpx.MockTransport(grid), pacing={})) == 1
    assert int(grid.requests[0]["min_timestamp"]) <= int(started.payment.created_at.timestamp() * 1000)


def _ton_tx(lt: int, utime: int, value: int = 0, memo: str = "", source: str = "EQsender") -> dict[str, Any]:
    return {
        "utime": utime,
        "transaction_id": {"lt": str(lt), "hash": f"h{lt}"},
        "in_msg": {"source": source, "value": str(value), "message": memo},
        "out_msgs": [],
    }


def _toncenter(items: list[dict[str, Any]], requests: list[dict[str, str]]):  # type: ignore[no-untyped-def]
    ordered = sorted(items, key=lambda i: -int(i["transaction_id"]["lt"]))

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        limit = int(params["limit"])
        start = 0
        if "lt" in params:  # Toncenter starts at the given transaction, inclusive
            assert params["hash"] == f"h{params['lt']}"
            start = next(i for i, row in enumerate(ordered) if row["transaction_id"]["lt"] == params["lt"])
        return httpx.Response(200, json={"ok": True, "result": ordered[start : start + limit]})

    return handler


async def test_ton_pages_backwards_by_lt_until_the_window_start() -> None:
    now = int(time.time())
    items = [_ton_tx(1000 - i, now - i, value=1) for i in range(250)]
    items.append(_ton_tx(10, now - 400, value=5_000_000_000, memo="P42"))
    items.append(_ton_tx(5, now - 10_000, value=7))  # older than the window: never returned
    requests: list[dict[str, str]] = []
    client = Client(httpx.MockTransport(_toncenter(items, requests)), pacing={})
    transfers = await client.incoming("ton", TON, now - 3_600)
    ids = [t.tx_id for t in transfers]
    assert "10:h10" in ids and "5:h5" not in ids
    assert len(ids) == len(set(ids)) == 251  # the inclusive start row is not duplicated
    assert "lt" not in requests[0] and requests[1]["lt"] == str(1000 - 99)


async def test_pagination_that_does_not_advance_is_an_error() -> None:
    def stuck(request: httpx.Request) -> httpx.Response:
        rows = [_trc20(f"s{i}", 1, 1_000_000 + i) for i in range(3)]
        return httpx.Response(200, json={"data": rows, "success": True, "meta": {"fingerprint": "same"}})

    client = Client(httpx.MockTransport(stuck), pacing={})
    first = await client.page("usdt_trc20", TRON, 0)
    assert first.next is not None
    try:
        await client.page("usdt_trc20", TRON, 0, cursor=first.next)
    except ChainError:
        pass
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a repeated continuation must not loop forever")


async def test_high_water_mark_in_the_future_is_ignored(session) -> None:  # type: ignore[no-untyped-def]
    shop = await _shop(session)
    cust = await factories.customer(session, shop)
    payment = await _payment(session, cust, 200_000, timedelta(minutes=10))
    now_ms = int(time.time() * 1000)
    future = {"hw": now_ms // 1000 + 86_400, "gaps": [], "retry": []}
    await kv.get_redis().set(watch.state_key(shop.id, "usdt_trc20", TRON), json.dumps(future))
    grid = FakeTronGrid([_trc20("r" * 64, payment.provider_amount or 0, now_ms - 60_000)])
    assert await watch.scan(session, Client(httpx.MockTransport(grid), pacing={})) == 1
