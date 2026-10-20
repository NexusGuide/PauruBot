"""Pure payload validation; no Telegram calls, storage or auth mocks."""

import io

import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from paurubot.security import ticket_files as files


def png() -> bytes:
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "remove me")
    Image.new("RGB", (4, 4), "red").save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def test_reencodes_images_and_strips_metadata() -> None:
    clean, mime, name = files.validate(png(), "image/png")
    assert mime == "image/png" and name == "attachment.png"
    with Image.open(io.BytesIO(clean)) as image:
        assert image.size == (4, 4) and "private" not in image.info


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"<svg></svg>", "image/svg+xml"),
        (b"<html>x</html>", "text/plain"),
        (b"<script>x</script>", "text/plain"),
        (b"PK\x03\x04archive", "application/zip"),
        (b"%PDF-1.7", "application/pdf"),
        (b"text\x00", "text/plain"),
        (b"\xff\xfe", "text/plain"),
        (b"fake", "image/png"),
        (png(), "image/jpeg"),
        (png()[:-20], "image/png"),
        (b"x" * (512 * 1024 + 1), "text/plain"),
    ],
    ids=[
        "svg",
        "html",
        "script",
        "archive",
        "pdf",
        "nul-text",
        "invalid-utf8",
        "fake-png",
        "mime-mismatch",
        "truncated-png",
        "oversized-text",
    ],
)
def test_rejects_active_binary_mismatched_and_malformed_payloads(data: bytes, mime: str) -> None:
    with pytest.raises(HTTPException) as error:
        files.validate(data, mime)
    assert error.value.status_code == 415


@pytest.mark.parametrize("data", [b"", b"x" * (files.MAX_BYTES + 1)], ids=["empty", "over-byte-limit"])
def test_hard_byte_limit(data: bytes) -> None:
    with pytest.raises(HTTPException) as error:
        files.validate(data, "text/plain")
    assert error.value.status_code == 413


def test_pixel_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(files, "MAX_PIXELS", 4)
    with pytest.raises(HTTPException):
        files.validate(png(), "image/png")


def test_plain_utf8_is_preserved() -> None:
    content = "سلام\nlog\tline".encode()
    assert files.validate(content, "text/plain; charset=utf-8") == (content, "text/plain", "attachment.txt")
