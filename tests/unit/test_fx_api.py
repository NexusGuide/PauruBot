"""ClockBot parity: crypto rates from the shop's own JSON API (URL, a path to the number, and a
divisor); Nobitex and manual rates stay as they were."""

from __future__ import annotations

import httpx
import pytest

from paurubot.payments import fx


def test_the_number_is_found_by_its_path() -> None:
    data = {"data": {"prices": [{"usdt": "62400.5"}]}}
    assert fx.pick(data, "data.prices.0.usdt") == 62400.5
    assert fx.pick(data, "data.missing") is None
    assert fx.pick({"x": True}, "x") is None


async def test_rates_from_the_shops_api(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        symbol = request.url.params.get("s")
        return httpx.Response(
            200, json={"result": {"price": {"usdt": 624000, "trx": 15000, "ton": 0}[symbol]}}
        )

    cfg = {
        "fx_source": "api",
        "fx_api_url": "https://rates.example.com/v1?s={symbol}",
        "fx_api_path": "result.price",
        "fx_api_divisor": 10,
        "fx_markup_percent": 0,
    }
    rates = await fx.fetch_api(cfg, transport=httpx.MockTransport(handler))
    assert rates == {"usdt": 62400, "trx": 1500}  # a zero price is no price
    assert seen[0] == "https://rates.example.com/v1?s=usdt"


async def test_the_api_source_reads_the_shops_cached_rates(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake(cfg: dict) -> dict[str, int]:  # type: ignore[type-arg]
        return {"usdt": 70000}

    monkeypatch.setattr(fx, "cached_api", fake)
    cfg = {"fx_source": "api", "fx_markup_percent": 10, "usdt_rate": 1}
    assert await fx.rate(cfg, "usdt") == 77000
    with pytest.raises(fx.RateUnavailable):
        await fx.rate({**cfg, "trx_rate": 0}, "trx")
