"""Exchange rates for crypto and dollar gateways (PAY-13).

Rates are Toman per whole coin (USDT, TRX, TON). The worker refreshes them from Nobitex every few
minutes into Redis; a shop can instead pin manual rates in its settings. A missing or stale rate
makes the crypto methods unavailable rather than guessing a price.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from paurubot import kv

log = logging.getLogger(__name__)

SYMBOLS = ("usdt", "trx", "ton")
NOBITEX = "https://api.nobitex.ir/market/stats"
MAX_AGE = 3 * 3600
_KEY = "fx:rates"


class RateUnavailable(Exception):
    pass


async def fetch_nobitex(transport: httpx.AsyncBaseTransport | None = None) -> dict[str, int]:
    """Latest Toman prices from Nobitex (quoted in Rial, so divided by ten)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=transport) as client:
        resp = await client.get(NOBITEX, params={"srcCurrency": ",".join(SYMBOLS), "dstCurrency": "rls"})
    resp.raise_for_status()
    stats = resp.json().get("stats") or {}
    rates: dict[str, int] = {}
    for symbol in SYMBOLS:
        entry = stats.get(f"{symbol}-rls") or {}
        try:
            rial = float(entry.get("latest") or entry.get("bestSell") or 0)
        except (TypeError, ValueError):
            continue
        if rial > 0:
            rates[symbol] = int(rial // 10)
    return rates


async def store(rates: dict[str, int]) -> None:
    payload = {"rates": rates, "at": time.time()}
    await kv.get_redis().set(_KEY, json.dumps(payload), ex=MAX_AGE * 2)


async def cached() -> dict[str, int]:
    raw = await kv.get_redis().get(_KEY)
    if not raw:
        return {}
    data = json.loads(raw)
    if time.time() - float(data.get("at") or 0) > MAX_AGE:
        return {}
    return {k: int(v) for k, v in (data.get("rates") or {}).items()}


async def refresh() -> dict[str, int]:
    try:
        rates = await fetch_nobitex()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("exchange rate refresh failed: %s", exc.__class__.__name__)
        return {}
    if rates:
        await store(rates)
    return rates


async def rate(cfg: dict[str, Any], symbol: str) -> int:
    """Toman per coin for a shop, including its markup. Raises RateUnavailable."""
    value = 0
    if cfg.get("fx_source") == "nobitex":
        value = (await cached()).get(symbol, 0)
    if not value:
        value = int(cfg.get(f"{symbol}_rate") or 0)
    if value <= 0:
        raise RateUnavailable(symbol)
    markup = max(0, int(cfg.get("fx_markup_percent") or 0))
    return value * (100 + markup) // 100
