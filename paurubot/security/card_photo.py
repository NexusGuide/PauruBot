"""Bounded, metadata-free card evidence. Original bytes are never persisted or served."""

from __future__ import annotations

import base64
import binascii
import io
import warnings

from PIL import Image, UnidentifiedImageError

MAX_PHOTO = 3 * 1024 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 12_000_000


class PhotoError(ValueError):
    pass


def decode_photo(encoded: str) -> bytes:
    if len(encoded) > 4 * ((MAX_PHOTO + 2) // 3):
        raise PhotoError("card_photo_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoError("card_photo_invalid") from None
    if not raw or len(raw) > MAX_PHOTO:
        raise PhotoError("card_photo_too_large")
    return raw


def sanitize_photo(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_PHOTO:
        raise PhotoError("card_photo_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                if (
                    probe.format not in {"JPEG", "PNG"}
                    or getattr(probe, "n_frames", 1) != 1
                    or max(probe.size) > MAX_SIDE
                    or probe.width * probe.height > MAX_PIXELS
                ):
                    raise PhotoError("card_photo_invalid")
                probe.verify()
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                # Paste pixels into a fresh image, not a copy carrying EXIF/text/profile metadata.
                clean = Image.new("RGB", source.size, "white")
                rgba = source.convert("RGBA")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=85)
                result = output.getvalue()
                if len(result) > MAX_PHOTO:
                    raise PhotoError("card_photo_too_large")
                return result
    except PhotoError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise PhotoError("card_photo_invalid") from None
