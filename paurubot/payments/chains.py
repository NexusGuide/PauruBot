"""Read incoming transfers from TRON (TronGrid) and TON (Toncenter) for on-chain payments (PAY-11).

Only confirmed transfers are reported. Amounts stay in integer base units (USDT and TRX use 6
decimals, TON uses 9), so no float ever touches money.

Both APIs return the newest transfers first, a page at a time (SEC-F10). `Client.page` reads one
page and returns two positions: `next` continues the same query in this run (TronGrid's fingerprint
is only valid with identical parameters), and `resume` is a plain restart point that a later run can
store and use. Pages are paced so a backfill stays inside the providers' free rate limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

TRONGRID = "https://api.trongrid.io"
TONCENTER = "https://toncenter.com/api/v2"
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

NETWORKS: dict[str, dict[str, Any]] = {
    "usdt_trc20": {"symbol": "usdt", "decimals": 6, "chain": "tron", "label": "USDT (TRC20)"},
    "trx": {"symbol": "trx", "decimals": 6, "chain": "tron", "label": "TRX"},
    "ton": {"symbol": "ton", "decimals": 9, "chain": "ton", "label": "TON"},
}

# How long after creation a payment can still be matched by an incoming transfer.
MATCH_WINDOW = timedelta(hours=24)
TRON_PAGE = 200
TON_PAGE = 100
# Seconds between page requests per chain, without / with an API key. Toncenter allows one
# request per second without a key; TronGrid throttles unkeyed callers hard.
PACING: dict[str, tuple[float, float]] = {"ton": (1.1, 0.12), "tron": (0.4, 0.08)}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ChainError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class Transfer:
    tx_id: str
    amount: int
    timestamp: int  # seconds
    memo: str = ""


@dataclass(slots=True, frozen=True)
class Page:
    """One API page. Timestamps cover every raw row, including rows that are not payments."""

    transfers: list[Transfer] = field(default_factory=list)
    newest: int | None = None
    oldest: int | None = None
    next: dict[str, Any] | None = None  # continue this query now; None once the window is read
    resume: dict[str, Any] | None = None  # JSON-safe restart point at `oldest` for a later run


def tron_hex(address: str) -> str:
    """Base58check TRON address to its 21-byte hex form (41...), validating the checksum."""
    number = 0
    for char in address:
        index = _B58.find(char)
        if index < 0:
            raise ChainError("invalid TRON address")
        number = number * 58 + index
    raw = number.to_bytes(25, "big")
    body, checksum = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != checksum or body[0] != 0x41:
        raise ChainError("invalid TRON address")
    return body.hex()


def valid_address(network: str, address: str) -> bool:
    if not address:
        return False
    if NETWORKS[network]["chain"] == "tron":
        try:
            tron_hex(address)
        except ChainError:
            return False
        return True
    return 46 <= len(address) <= 68 and address.replace("-", "").replace("_", "").replace(":", "").isalnum()


class Client:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        pacing: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        self._transport = transport
        self._pacing = PACING if pacing is None else pacing
        self._last: dict[str, float] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), transport=self._transport)

    async def _pace(self, chain: str, keyed: bool) -> None:
        interval = self._pacing.get(chain, (0.0, 0.0))[1 if keyed else 0]
        last = self._last.get(chain)
        if last is not None and interval > 0:
            wait = last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last[chain] = time.monotonic()

    async def page(
        self,
        network: str,
        address: str,
        since: int,
        api_key: str = "",
        cursor: Mapping[str, Any] | None = None,
    ) -> Page:
        """One newest-first page of confirmed incoming transfers at or after `since` (seconds)."""
        if network not in NETWORKS:
            raise ChainError(f"unknown network {network}")
        chain = NETWORKS[network]["chain"]
        await self._pace(chain, bool(api_key))
        try:
            if chain == "tron":
                result = await self._tron_page(network, address, since, api_key, cursor or {})
            else:
                result = await self._ton_page(address, since, api_key, cursor or {})
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ChainError(f"{network}: {exc.__class__.__name__}") from exc
        if result.next is not None and cursor and dict(result.next) == dict(cursor):
            raise ChainError(f"{network}: pagination did not advance")
        return result

    async def incoming(
        self, network: str, address: str, since: int, api_key: str = "", max_pages: int = 10
    ) -> list[Transfer]:
        """Every transfer since `since`, reading at most `max_pages` pages (newest first)."""
        found: dict[str, Transfer] = {}
        cursor: dict[str, Any] | None = None
        for _ in range(max_pages):
            result = await self.page(network, address, since, api_key, cursor)
            for transfer in result.transfers:
                found.setdefault(transfer.tx_id, transfer)
            if result.next is None:
                break
            cursor = result.next
        return list(found.values())

    async def _tron_page(
        self, network: str, address: str, since: int, api_key: str, cursor: Mapping[str, Any]
    ) -> Page:
        headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        params: dict[str, str | int] = {
            "only_to": "true",
            "only_confirmed": "true",
            "min_timestamp": since * 1000,
            "order_by": "block_timestamp,desc",
            "limit": TRON_PAGE,
        }
        if network == "usdt_trc20":
            params["contract_address"] = USDT_TRC20
            path = f"{TRONGRID}/v1/accounts/{address}/transactions/trc20"
        else:
            path = f"{TRONGRID}/v1/accounts/{address}/transactions"
        if cursor.get("max_ms") is not None:
            params["max_timestamp"] = int(cursor["max_ms"])
        if cursor.get("fp"):
            params["fingerprint"] = str(cursor["fp"])
        async with self._http() as client:
            resp = await client.get(path, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if body.get("success") is False:
            raise ChainError(f"{network}: trongrid error")
        rows = body.get("data") or []
        parse = self._trc20_row if network == "usdt_trc20" else self._trx_row
        target = tron_hex(address)
        transfers = [t for row in rows if (t := parse(row, address, target)) is not None]
        stamps = [int(row["block_timestamp"]) for row in rows]
        fingerprint = (body.get("meta") or {}).get("fingerprint")
        if not stamps:
            return Page(transfers)
        return Page(
            transfers,
            newest=max(stamps) // 1000,
            oldest=min(stamps) // 1000,
            # A fingerprint is only valid with the parameters that produced it.
            next={"max_ms": cursor.get("max_ms"), "fp": str(fingerprint)} if fingerprint else None,
            # Restart inclusively at the oldest row; a re-read row is skipped by its tx id.
            resume={"max_ms": min(stamps)},
        )

    @staticmethod
    def _trc20_row(item: dict[str, Any], address: str, _target: str) -> Transfer | None:
        token = item.get("token_info") or {}
        if item.get("to") != address or token.get("address") != USDT_TRC20 or item.get("type") != "Transfer":
            return None
        if int(token.get("decimals", -1)) != 6:
            return None
        return Transfer(str(item["transaction_id"]), int(item["value"]), int(item["block_timestamp"]) // 1000)

    @staticmethod
    def _trx_row(item: dict[str, Any], _address: str, target: str) -> Transfer | None:
        if (item.get("ret") or [{}])[0].get("contractRet") != "SUCCESS":
            return None
        contracts = (item.get("raw_data") or {}).get("contract") or []
        if len(contracts) != 1 or contracts[0].get("type") != "TransferContract":
            return None
        value = (contracts[0].get("parameter") or {}).get("value") or {}
        if str(value.get("to_address", "")).lower() != target:
            return None
        return Transfer(str(item["txID"]), int(value["amount"]), int(item["block_timestamp"]) // 1000)

    async def _ton_page(self, address: str, since: int, api_key: str, cursor: Mapping[str, Any]) -> Page:
        headers = {"X-API-Key": api_key} if api_key else {}
        params: dict[str, str | int] = {"address": address, "limit": TON_PAGE}
        if cursor.get("lt") is not None:
            # Toncenter starts at this transaction (inclusive) and walks back in time.
            params["lt"] = str(cursor["lt"])
            params["hash"] = str(cursor["hash"])
        async with self._http() as client:
            resp = await client.get(f"{TONCENTER}/getTransactions", params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise ChainError("toncenter error")
        rows = body.get("result") or []
        transfers = []
        for item in rows:
            utime = int(item.get("utime") or 0)
            if utime < since:
                continue
            message = item.get("in_msg") or {}
            if not message.get("source") or int(message.get("value") or 0) <= 0:
                continue  # external or zero-value message, not a payment
            if item.get("out_msgs"):
                continue  # bounced back
            tx = item.get("transaction_id") or {}
            transfers.append(
                Transfer(
                    f"{tx.get('lt')}:{tx.get('hash')}",
                    int(message["value"]),
                    utime,
                    str(message.get("message") or ""),
                )
            )
        if not rows:
            return Page(transfers)
        stamps = [int(row.get("utime") or 0) for row in rows]
        last = rows[-1].get("transaction_id") or {}
        position = {"lt": str(last["lt"]), "hash": str(last["hash"])}
        more = len(rows) >= TON_PAGE and min(stamps) >= since
        return Page(
            transfers,
            newest=max(stamps),
            oldest=min(stamps),
            next=position if more else None,
            resume=position,
        )
