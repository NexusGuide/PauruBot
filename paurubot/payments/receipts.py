"""Receipt fingerprints for duplicate detection (PAY-06): exact SHA-256 and a 64-bit difference hash."""

from __future__ import annotations

import hashlib
import io

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Payment

PHASH_MAX_DISTANCE = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dhash(data: bytes, size: int = 8) -> str | None:
    """Perceptual difference hash; robust to re-compression and small resizes."""
    if len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.draft("L", (size * 16, size * 16))
            gray = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            pixels = list(gray.tobytes())
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


async def find_duplicate(
    session: AsyncSession, shop_id: int, payment_id: int, digest: str | None, phash: str | None
) -> Payment | None:
    """An earlier receipt with the same bytes, or a visually near-identical image."""
    if digest:
        exact = await session.scalar(
            select(Payment)
            .where(Payment.shop_id == shop_id, Payment.id != payment_id, Payment.receipt_sha256 == digest)
            .order_by(Payment.id)
            .limit(1)
        )
        if exact is not None:
            return exact
    if not phash:
        return None
    candidates = await session.execute(
        select(Payment.id, Payment.receipt_phash)
        .where(Payment.shop_id == shop_id, Payment.id != payment_id, Payment.receipt_phash.is_not(None))
        .order_by(Payment.id.desc())
        .limit(5000)
    )
    for other_id, other_hash in candidates.all():
        if other_hash and distance(phash, other_hash) <= PHASH_MAX_DISTANCE:
            return await session.get(Payment, other_id)
    return None
