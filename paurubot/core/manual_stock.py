"""Versioned encrypted deliveries. No fetching or execution of imported content."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import stock
from paurubot.db.models import Plan, Shop, StockItem
from paurubot.security import secretbox

PREFIX = "PAURU-STOCK-V1\n"
MAX_FILE = 1024 * 1024
MAX_IMPORT = 8 * 1024 * 1024
STORAGE_LIMIT = 10 * 1024**3
EXTENSIONS = {"txt", "conf", "ovpn", "json", "yaml", "yml", "npv", "pcf", "ehi"}


class Part(BaseModel):
    text: str = Field(default="", max_length=MAX_FILE)
    filename: str | None = Field(default=None, max_length=96)
    data: str | None = Field(default=None, max_length=1_398_104)
    as_file: bool = False
    subscription_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_content(self) -> Part:
        if self.data is not None:
            if self.text or not self.filename:
                raise ValueError("file_requires_filename")
            try:
                decoded = base64.b64decode(self.data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid_file") from exc
            if not decoded or len(decoded) > MAX_FILE:
                raise ValueError("file_size")
        elif not self.text.strip() or len(self.text.encode()) > MAX_FILE:
            raise ValueError("text_size")
        if self.filename:
            # No paths, bidi/control characters, executable or active browser formats.
            if not re.fullmatch(r"[\w .()\-]{1,90}\.[A-Za-z0-9]{1,5}", self.filename):
                raise ValueError("invalid_filename")
            if self.filename.rsplit(".", 1)[-1].lower() not in EXTENSIONS:
                raise ValueError("file_type")
        if self.subscription_url:
            u = urlsplit(self.subscription_url)
            if (
                u.scheme not in {"https", "http"}
                or not u.hostname
                or u.username
                or u.password
                or any(ord(c) < 33 or ord(c) == 127 for c in self.subscription_url)
            ):
                raise ValueError("invalid_subscription_url")
        return self

    def content(self) -> bytes:
        return base64.b64decode(self.data) if self.data is not None else self.text.encode()


class Import(BaseModel):
    plan_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=80)
    mode: Literal["single", "bundle", "bulk"] = "single"
    parts: list[Part] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded(self) -> Import:
        if self.mode == "single" and len(self.parts) != 1:
            raise ValueError("single_requires_one_part")
        if self.mode == "bundle" and len(self.parts) > 20:
            raise ValueError("bundle_too_many")
        if sum(len(p.content()) for p in self.parts) > MAX_IMPORT:
            raise ValueError("import_too_large")
        if not self.name.strip() or any(ord(c) < 32 for c in self.name):
            raise ValueError("invalid_name")
        return self


def encode(parts: list[Part]) -> str:
    return PREFIX + json.dumps(
        [p.model_dump(exclude_defaults=True) for p in parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode(value: str) -> list[Part]:
    if not value.startswith(PREFIX):
        return [Part(text=value)]
    return [Part.model_validate(p) for p in json.loads(value[len(PREFIX) :])]


def parts(item: StockItem) -> list[Part]:
    return decode(stock.raw_payload(item))


def display(value: str) -> str:
    """Safe, human-readable compatibility view; never expose the internal envelope."""
    return "\n\n".join(
        (p.text if p.data is None else f"[file: {p.filename}]")
        + (f"\n{p.subscription_url}" if p.subscription_url else "")
        for p in decode(value)
    )


async def storage_used(db: AsyncSession, shop_id: int) -> int:
    # Include legacy entries too; ciphertext is a conservative upper bound for those.
    return int(
        await db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            func.nullif(StockItem.content_bytes, 0), func.length(StockItem.payload_enc)
                        )
                    ),
                    0,
                )
            ).where(StockItem.shop_id == shop_id)
        )
        or 0
    )


async def add(db: AsyncSession, plan: Plan, body: Import) -> dict[str, int]:
    if plan.delivery != "stock" or plan.id != body.plan_id:
        raise stock.StockError("plan_unavailable")
    # Serialize ingestion with legacy imports: quota and duplicate checks are one transaction.
    await db.scalar(select(Shop.id).where(Shop.id == plan.shop_id).with_for_update())
    groups = [[p] for p in body.parts] if body.mode == "bulk" else [body.parts]
    used = await storage_used(db, plan.shop_id)
    added = duplicates = 0
    for index, group in enumerate(groups, 1):
        value = encode(group)
        encrypted = secretbox.encrypt(stock.PURPOSE, value)
        size = len(encrypted.encode())
        # Identity follows bytes, not filenames or delivery/display options.
        # A UTF-8 file and the same legacy text must not become two sale units.
        identities = []
        for part in group:
            raw = part.content()
            try:
                identities.append(raw.decode("utf-8").strip())
            except UnicodeDecodeError:
                identities.append("binary:" + base64.b64encode(raw).decode())
        digest_value = (
            identities[0] if len(identities) == 1 else json.dumps(sorted(identities), ensure_ascii=False)
        )
        digest = stock._digest(plan.shop_id, digest_value)
        if await db.scalar(
            select(StockItem.id).where(StockItem.shop_id == plan.shop_id, StockItem.digest == digest)
        ):
            duplicates += 1
            continue
        if used + size > STORAGE_LIMIT:
            raise stock.StockError("stock_storage_full")
        result = await db.execute(
            insert(StockItem)
            .values(
                shop_id=plan.shop_id,
                plan_id=plan.id,
                name=body.name.strip() + (f"-{index}" if body.mode == "bulk" else ""),
                content_bytes=size,
                part_count=len(group),
                payload_enc=encrypted,
                digest=digest,
                status=stock.AVAILABLE,
            )
            .on_conflict_do_nothing(index_elements=[StockItem.shop_id, StockItem.digest])
            .returning(StockItem.id)
        )
        if result.scalar_one_or_none() is None:
            duplicates += 1
        else:
            used += size
            added += 1
    return {"added": added, "duplicates": duplicates}
