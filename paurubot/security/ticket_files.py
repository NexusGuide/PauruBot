"""Decode support images and allow only bounded plain-text documents."""

from __future__ import annotations

import io
import warnings

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

MAX_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 12_000_000


def validate(data: bytes, mime: str) -> tuple[bytes, str, str]:
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="ticket_file_too_large")
    mime = mime.split(";", 1)[0].strip().lower()
    if mime in {"image/png", "image/jpeg"}:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    expected = "PNG" if mime == "image/png" else "JPEG"
                    if image.format != expected or image.width * image.height > MAX_PIXELS:
                        raise ValueError("image format or pixel limit")
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    out = io.BytesIO()
                    clean = Image.new("RGB", image.size)
                    clean.paste(image.convert("RGB"))
                    clean.save(out, format=expected)
                    result = out.getvalue()
                    if len(result) > MAX_BYTES:
                        raise HTTPException(status_code=413, detail="ticket_file_too_large")
                    return result, mime, "attachment.png" if expected == "PNG" else "attachment.jpg"
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ) as exc:
            raise HTTPException(status_code=415, detail="ticket_file_type") from exc
    if mime == "text/plain":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise HTTPException(status_code=415, detail="ticket_file_type") from exc
        if len(data) > 512 * 1024 or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise HTTPException(status_code=415, detail="ticket_file_type")
        if any(tag in text.lower() for tag in ("<html", "<!doctype", "<svg", "<script", "<?xml")):
            raise HTTPException(status_code=415, detail="ticket_file_type")
        return text.encode(), "text/plain", "attachment.txt"
    raise HTTPException(status_code=415, detail="ticket_file_type")
