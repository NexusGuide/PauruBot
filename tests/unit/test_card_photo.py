from __future__ import annotations

import base64
import io

import pytest
from PIL import Image, PngImagePlugin

from paurubot.security.card_photo import MAX_PHOTO, PhotoError, decode_photo, sanitize_photo


def picture(fmt="PNG", size=(10, 10)):  # type: ignore[no-untyped-def]
    output = io.BytesIO()
    image = Image.new("RGB", size, "red")
    meta = PngImagePlugin.PngInfo()
    meta.add_text("private", "sensitive-metadata")
    image.save(output, format=fmt, pnginfo=meta)
    return output.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_photo_is_decoded_and_reencoded_without_metadata(fmt) -> None:  # type: ignore[no-untyped-def]
    raw = picture(fmt)
    assert decode_photo(base64.b64encode(raw).decode()) == raw
    clean = sanitize_photo(raw)
    assert b"sensitive-metadata" not in clean
    with Image.open(io.BytesIO(clean)) as image:
        assert image.format == "JPEG" and image.size == (10, 10) and not image.getexif()


@pytest.mark.parametrize(
    "raw",
    [b"", b"not-image", b"<svg/>", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n", b"x" * (MAX_PHOTO + 1)],
    ids=["empty", "text", "svg", "pdf", "truncated", "oversize"],
)
def test_rejects_malformed_or_large_photo(raw) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PhotoError):
        sanitize_photo(raw)


@pytest.mark.parametrize(
    "value",
    ["data:image/png;base64,eA==", "%%%", "e A==", "", "eA==" * (MAX_PHOTO // 3 + 1)],
    ids=["data-uri", "bad", "space", "empty", "oversize"],
)
def test_strict_bounded_base64(value) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PhotoError):
        decode_photo(value)


def test_side_and_format_limits() -> None:
    with pytest.raises(PhotoError):
        sanitize_photo(picture(size=(4097, 1)))
    with pytest.raises(PhotoError):
        sanitize_photo(picture("GIF"))


def test_animated_png_rejected() -> None:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        output, format="PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")]
    )
    with pytest.raises(PhotoError):
        sanitize_photo(output.getvalue())
