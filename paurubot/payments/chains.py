"""Read incoming transfers from TRON (TronGrid) and TON (Toncenter) for on-chain payments (PAY-11).

Only confirmed transfers are reported. Amounts stay in integer base units (USDT and TRX use 6
decimals, TON uses 9), so no float ever touches money.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ChainError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class Transfer:
    tx_id: str
    amount: int
    timestamp: int  # seconds
    memo: str = ""


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
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), transport=self._transport)

    async def incoming(self, network: str, address: str, since: int, api_key: str = "") -> list[Transfer]:
        chain = NETWORKS[network]["chain"]
        try:
            if network == "usdt_trc20":
                return await self._trc20(address, since, api_key)
            if network == "trx":
                return await self._trx(address, since, api_key)
            if chain == "ton":
                return await self._ton(address, since, api_key)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ChainError(f"{network}: {exc.__class__.__name__}") from exc
        raise ChainError(f"unknown network {network}")

    async def _trc20(self, address: str, since: int, api_key: str) -> list[Transfer]:
        headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        params: dict[str, str | int] = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": USDT_TRC20,
            "min_timestamp": since * 1000,
            "limit": 200,
        }
        async with self._http() as client:
            resp = await client.get(
                f"{TRONGRID}/v1/accounts/{address}/transactions/trc20", params=params, headers=headers
            )
        resp.raise_for_status()
        transfers = []
        for item in resp.json().get("data") or []:
            token = item.get("token_info") or {}
            if (
                item.get("to") != address
                or token.get("address") != USDT_TRC20
                or item.get("type") != "Transfer"
            ):
                continue
            if int(token.get("decimals", -1)) != 6:
                continue
            transfers.append(
                Transfer(
                    str(item["transaction_id"]), int(item["value"]), int(item["block_timestamp"]) // 1000
                )
            )
        return transfers

    async def _trx(self, address: str, since: int, api_key: str) -> list[Transfer]:
        headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        params: dict[str, str | int] = {
            "only_to": "true",
            "only_confirmed": "true",
            "min_timestamp": since * 1000,
            "limit": 200,
        }
        target = tron_hex(address)
        async with self._http() as client:
            resp = await client.get(
                f"{TRONGRID}/v1/accounts/{address}/transactions", params=params, headers=headers
            )
        resp.raise_for_status()
        transfers = []
        for item in resp.json().get("data") or []:
            if (item.get("ret") or [{}])[0].get("contractRet") != "SUCCESS":
                continue
            contracts = (item.get("raw_data") or {}).get("contract") or []
            if len(contracts) != 1 or contracts[0].get("type") != "TransferContract":
                continue
            value = (contracts[0].get("parameter") or {}).get("value") or {}
            if str(value.get("to_address", "")).lower() != target:
                continue
            transfers.append(
                Transfer(str(item["txID"]), int(value["amount"]), int(item["block_timestamp"]) // 1000)
            )
        return transfers

    async def _ton(self, address: str, since: int, api_key: str) -> list[Transfer]:
        headers = {"X-API-Key": api_key} if api_key else {}
        async with self._http() as client:
            resp = await client.get(
                f"{TONCENTER}/getTransactions", params={"address": address, "limit": 100}, headers=headers
            )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise ChainError("toncenter error")
        transfers = []
        for item in body.get("result") or []:
            message = item.get("in_msg") or {}
            if not message.get("source") or int(message.get("value") or 0) <= 0:
                continue  # external or zero-value message, not a payment
            if item.get("out_msgs"):
                continue  # bounced back
            utime = int(item.get("utime") or 0)
            if utime < since:
                continue
            tx = item.get("transaction_id") or {}
            transfers.append(
                Transfer(
                    f"{tx.get('lt')}:{tx.get('hash')}",
                    int(message["value"]),
                    utime,
                    str(message.get("message") or ""),
                )
            )
        return transfers
