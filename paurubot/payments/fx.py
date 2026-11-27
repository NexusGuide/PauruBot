"""Exchange rates for crypto and dollar gateways (PAY-13).

Rates are Toman per whole coin (USDT, TRX, TON). The worker refreshes them from Nobitex every few
minutes into Redis; a shop can instead pin manual rates in its settings. A missing or stale rate
makes the crypto methods unavailable rather than guessing a price.
"""

from __future__ import annotations

import json
import logging
import math
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
        if math.isfinite(rial) and rial > 0:
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


def pick(data: Any, path: str) -> float | None:
    """The number at a dotted path in JSON (`data.prices.0.usdt`); None when it isn't one."""
    node = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    if isinstance(node, bool):
        return None
    try:
        value = float(node)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def api_key(cfg: dict[str, Any]) -> str:
    import hashlib

    raw = f"{cfg.get('fx_api_url')}|{cfg.get('fx_api_path')}|{cfg.get('fx_api_divisor')}"
    return f"fx:api:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


async def fetch_api(cfg: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> dict[str, int]:
    """Toman per coin from the shop's own JSON API (ClockBot parity): `{symbol}` in the URL or
    path is each coin; the number found is divided by the divisor (10 for a Rial price)."""
    import ssl

    from paurubot.security.http import ValidatedTransport

    template = str(cfg.get("fx_api_url") or "")
    path = str(cfg.get("fx_api_path") or "")
    divisor = max(1, int(cfg.get("fx_api_divisor") or 1))
    if not template.startswith("https://") or not path:
        return {}
    rates: dict[str, int] = {}
    payloads: dict[str, Any] = {}
    failed_urls: set[str] = set()
    for symbol in SYMBOLS:
        url = template.replace("{symbol}", symbol)
        if url in failed_urls:
            continue
        if url not in payloads:
            try:
                client_transport = transport or ValidatedTransport(
                    url, context=ssl.create_default_context(), allow_private=False
                )
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10, connect=5),
                    transport=client_transport,
                    follow_redirects=False,
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    payloads[url] = resp.json()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                failed_urls.add(url)
                continue
        value = pick(payloads[url], path.replace("{symbol}", symbol))
        if value and value > 0:
            rates[symbol] = int(value // divisor)
    return rates


async def cached_api(cfg: dict[str, Any]) -> dict[str, int]:
    raw = await kv.get_redis().get(api_key(cfg))
    if not raw:
        return {}
    data = json.loads(raw)
    if time.time() - float(data.get("at") or 0) > MAX_AGE:
        return {}
    return {k: int(v) for k, v in (data.get("rates") or {}).items()}


async def refresh_api(cfg: dict[str, Any]) -> dict[str, int]:
    rates = await fetch_api(cfg)
    if rates:
        await kv.get_redis().set(
            api_key(cfg), json.dumps({"rates": rates, "at": time.time()}), ex=MAX_AGE * 2
        )
    return rates


async def rate(cfg: dict[str, Any], symbol: str) -> int:
    """Toman per coin for a shop, including its markup. Raises RateUnavailable."""
    value = 0
    if cfg.get("fx_source") == "nobitex":
        value = (await cached()).get(symbol, 0)
    elif cfg.get("fx_source") == "api":
        value = (await cached_api(cfg)).get(symbol, 0)
    if not value:
        value = int(cfg.get(f"{symbol}_rate") or 0)
    if value <= 0:
        raise RateUnavailable(symbol)
    markup = max(0, int(cfg.get("fx_markup_percent") or 0))
    return value * (100 + markup) // 100
