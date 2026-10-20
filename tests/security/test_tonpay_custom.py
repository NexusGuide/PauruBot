"""Unregistered custom-client contract tests, fake HTTP and synthetic receipt pixels."""

from __future__ import annotations

import ast
import io
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from PIL import Image

from paurubot.payments.providers.tonpay_custom import (
    RECEIPT_LIMIT,
    CustomAPIError,
    TonpayCustomClient,
    receipt_image,
)

pytestmark = pytest.mark.security
KEY = "synthetic-custom-key"
CALLBACK = "https://shop.example.com/api/pay/gw/tonpay_custom?pid=42"


def invoice(**changes):  # type: ignore[no-untyped-def]
    return {
        "invoice_id": "invoice_42",
        "order_id": "42",
        "request_amount": 100_000,
        "final_amount": 103_000,
        "status": "pending",
        "paid": False,
        "card_number": "0000-0000-0000-0000",
        "card_name": "Synthetic recipient",
        **changes,
    }


def client(data=None, status=200, headers=None):  # type: ignore[no-untyped-def]
    calls = []

    def handle(request):  # type: ignore[no-untyped-def]
        # Multipart streams must be read to inspect the captured body.
        request.read()
        calls.append(request)
        return httpx.Response(status, json=invoice() if data is None else data, headers=headers)

    return TonpayCustomClient(KEY, transport=httpx.MockTransport(handle)), calls


def pixels(fmt="PNG"):  # type: ignore[no-untyped-def]
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (25, 60, 100)).save(stream, format=fmt)
    return stream.getvalue()


async def test_create_requires_backend_principal_and_toman_contract():
    api, calls = client()
    result = await api.create("42", 100_000, 123_456_789, CALLBACK)
    assert result.request_amount == 100_000
    assert result.payable_amount == 103_000 and result.fee == 3_000
    assert not result.paid
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "https://tonpays.online/api/custom/v1/invoices/telegram/create"
    assert request.headers["X-API-Key"] == KEY
    assert json.loads(request.content) == {
        "order_id": "42",
        "amount": 100_000,
        "buyer_chat_id": 123_456_789,
        "callback_url": CALLBACK,
    }
    with pytest.raises(FrozenInstanceError):
        result.payable_amount = 1
    assert result.card_number not in repr(result)
    assert result.card_name not in repr(result)
    assert KEY not in repr(api)


async def test_create_without_remote_order_does_not_fabricate_identity():
    data = invoice()
    del data["order_id"]
    api, _ = client(data)
    assert (await api.create("42", 100_000, 123, CALLBACK)).order_id is None
    with pytest.raises(CustomAPIError) as exc:
        await api.check("invoice_42", "42", 100_000)
    assert exc.value.code == "identity_mismatch"


@pytest.mark.parametrize("buyer", [0, -1, True, "123", 2**63])
async def test_invalid_buyer_never_calls_provider(buyer):  # type: ignore[no-untyped-def]
    api, calls = client()
    with pytest.raises(CustomAPIError):
        await api.create("42", 100_000, buyer, CALLBACK)
    assert not calls


@pytest.mark.parametrize("amount", [0, -1, True, "100000", 100000.0, 10**18])
async def test_invalid_local_amount_never_calls_provider(amount):  # type: ignore[no-untyped-def]
    api, calls = client()
    with pytest.raises(CustomAPIError):
        await api.create("42", amount, 123, CALLBACK)
    assert not calls


@pytest.mark.parametrize(
    "callback",
    [
        "http://shop.example.com/cb",
        "https://127.0.0.1/cb",
        "https://10.0.0.1/cb",
        "https://localhost/cb",
        "https://service.internal/cb",
        "https://user:pass@shop.example.com/cb",
        "https://shop.example.com:444/cb",
        "https://shop.example.com/cb#fragment",
        "https://shop.example.com/\nsecret",
        "https://shop.example.com\\@evil.example/cb",
    ],
)
async def test_invalid_callback_never_calls_provider(callback):  # type: ignore[no-untyped-def]
    api, calls = client()
    with pytest.raises(CustomAPIError):
        await api.create("42", 100_000, 123, callback)
    assert not calls


@pytest.mark.parametrize(
    "changes",
    [
        {"invoice_id": ".."},
        {"invoice_id": "../../create"},
        {"invoice_id": True},
        {"order_id": "43"},
        {"order_id": True},
        {"request_amount": 99_999},
        {"request_amount": True},
        {"final_amount": 99_999},
        {"final_amount": None},
        {"final_amount": "NaN"},
        {"final_amount": "103000.5"},
        {"final_amount": "1e5"},
        {"final_amount": 103000.0},
        {"currency": "IRR"},
        {"paid": "false"},
        {"paid": 1},
        {"receipt_received": "true"},
        {"change_card_exhausted": 1},
        {"card_name": "x" * 121},
        {"card_number": "x" * 41},
        {"card_name": "a\nb"},
    ],
)
async def test_malformed_remote_invoice_fails_closed(changes):  # type: ignore[no-untyped-def]
    api, _ = client(invoice(**changes))
    with pytest.raises(CustomAPIError):
        await api.check("invoice_42", "42", 100_000)


async def test_check_uses_stored_identity_and_exposes_fee_without_wallet_verification():
    api, calls = client(invoice(paid=True, status="completed"))
    result = await api.check("invoice_42", "42", 100_000)
    assert calls[0].method == "GET"
    assert str(calls[0].url) == "https://tonpays.online/api/custom/v1/invoices/check/invoice_42"
    assert result.paid and result.status == "completed"
    assert result.request_amount == 100_000 and result.payable_amount == 103_000
    assert not hasattr(result, "provider_tx_id")


@pytest.mark.parametrize(
    "state", ["pending", "processing", "need_action", "rejected", "expired", "future_state"]
)
async def test_status_projection_does_not_infer_payment(state):  # type: ignore[no-untyped-def]
    api, _ = client(invoice(status=state))
    result = await api.check("invoice_42", "42", 100_000)
    assert result.status == state and not result.paid


async def test_different_invoice_rejected_even_if_order_and_amount_match():
    api, _ = client(invoice(invoice_id="different"))
    with pytest.raises(CustomAPIError) as exc:
        await api.check("invoice_42", "42", 100_000)
    assert exc.value.code == "identity_mismatch"


@pytest.mark.parametrize("reference", ["", "..", "../other", "x/y", "x?key=y", "x%2Fy", "x" * 129])
async def test_unsafe_identity_never_enters_api_path(reference):  # type: ignore[no-untyped-def]
    api, calls = client()
    for action in [
        api.check(reference, "42", 100_000),
        api.change_card(reference, "42", 100_000),
        api.submit_receipt(reference, "42", 100_000, pixels()),
    ]:
        with pytest.raises(CustomAPIError):
            await action
    assert not calls


async def test_card_change_carries_identity_and_does_not_hide_fee_change():
    api, calls = client(invoice(final_amount=104_000, change_card_exhausted=True))
    result = await api.change_card("invoice_42", "42", 100_000)
    assert result.payable_amount == 104_000 and result.fee == 4_000
    assert result.change_card_exhausted
    assert calls[0].method == "POST"
    assert str(calls[0].url).endswith("/invoices/invoice_42/change-card")


async def test_card_change_without_card_is_invalid():
    api, _ = client(invoice(card_number=""))
    with pytest.raises(CustomAPIError):
        await api.change_card("invoice_42", "42", 100_000)


@pytest.mark.parametrize(
    "fmt,mime,ext",
    [("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png"), ("WEBP", "image/webp", "webp")],
)
async def test_receipt_multipart_is_image_only_and_ack_not_settlement(fmt, mime, ext):  # type: ignore[no-untyped-def]
    body = pixels(fmt)
    api, calls = client(invoice(status="processing", receipt_received=True))
    result = await api.submit_receipt("invoice_42", "42", 100_000, body)
    assert result.receipt_received and not result.paid
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url).endswith("/invoices/invoice_42/receipt")
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert f'filename="receipt.{ext}"'.encode() in request.content
    assert f"Content-Type: {mime}".encode() in request.content
    assert body in request.content
    assert KEY.encode() not in request.content
    image = receipt_image(body)
    assert image.content == body and "content=" not in repr(image)
    assert len(repr(image)) < 120


@pytest.mark.parametrize("received", [False, None, "true", 1])
async def test_receipt_ack_must_be_explicit_boolean(received):  # type: ignore[no-untyped-def]
    api, _ = client(invoice(receipt_received=received))
    with pytest.raises(CustomAPIError):
        await api.submit_receipt("invoice_42", "42", 100_000, pixels())


@pytest.mark.parametrize(
    "body",
    [b"", b"<svg></svg>", b"%PDF-1.7", b"not an image", b"\x89PNG\r\n\x1a\n", b"x" * (RECEIPT_LIMIT + 1)],
    ids=["empty", "svg", "pdf", "non-image", "truncated-png", "over-byte-limit"],
)
async def test_invalid_receipt_never_leaves_server(body):  # type: ignore[no-untyped-def]
    api, calls = client()
    with pytest.raises(CustomAPIError):
        await api.submit_receipt("invoice_42", "42", 100_000, body)
    assert not calls


def test_truncated_jpeg_and_animation_rejected():
    with pytest.raises(CustomAPIError):
        receipt_image(pixels("JPEG")[:-20])
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(
        data, format="PNG", save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")]
    )
    with pytest.raises(CustomAPIError):
        receipt_image(data.getvalue())


def test_pixel_limit_checked_before_decode(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("paurubot.payments.providers.tonpay_custom.PIXEL_LIMIT", 32)
    with pytest.raises(CustomAPIError):
        receipt_image(pixels())


@pytest.mark.parametrize("hint,expected", [("30", 30), ("99999999", 3600), ("-5", None), ("tomorrow", None)])
async def test_rate_limit_reports_bounded_hint_without_retry(hint, expected):  # type: ignore[no-untyped-def]
    api, calls = client(status=429, headers={"Retry-After": hint})
    with pytest.raises(CustomAPIError) as exc:
        await api.check("invoice_42", "42", 100_000)
    assert exc.value.code == "rate_limited" and exc.value.retry_after == expected
    assert len(calls) == 1


@pytest.mark.parametrize("status", [301, 302, 401, 404, 500])
async def test_http_failure_never_redirects_or_retries_mutation(status):  # type: ignore[no-untyped-def]
    api, calls = client(status=status, headers={"Location": "https://evil.example/steal"})
    with pytest.raises(CustomAPIError):
        await api.change_card("invoice_42", "42", 100_000)
    assert len(calls) == 1 and calls[0].url.host == "tonpays.online"


@pytest.mark.parametrize(
    "body",
    [b"[]", b"null", b"not json", b'{"paid":false,"paid":true}', b" " * 65537],
    ids=["array", "null", "invalid-json", "duplicate-key", "oversized-json"],
)
async def test_invalid_or_ambiguous_json_never_accepted(body):  # type: ignore[no-untyped-def]
    api = TonpayCustomClient(KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
    with pytest.raises(CustomAPIError):
        await api.check("invoice_42", "42", 100_000)


async def test_stream_limit_stops_before_consuming_unbounded_body():
    chunks = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for index in range(10):
                chunks.append(index)
                yield b" " * 32768

    api = TonpayCustomClient(
        KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks()))
    )
    with pytest.raises(CustomAPIError) as exc:
        await api.check("invoice_42", "42", 100_000)
    assert exc.value.code == "response_too_large" and chunks == [0, 1, 2]


@pytest.mark.parametrize("operation", ["create", "change_card", "submit_receipt"])
async def test_ambiguous_mutation_is_single_attempt_and_error_sanitized(operation, caplog):  # type: ignore[no-untyped-def]
    calls = []

    def timeout(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        raise httpx.ReadTimeout(KEY, request=request)

    api = TonpayCustomClient(KEY, transport=httpx.MockTransport(timeout))
    with pytest.raises(CustomAPIError) as exc:
        if operation == "create":
            await api.create("42", 100_000, 123, CALLBACK)
        elif operation == "change_card":
            await api.change_card("invoice_42", "42", 100_000)
        else:
            await api.submit_receipt("invoice_42", "42", 100_000, pixels())
    assert len(calls) == 1 and KEY not in str(exc.value) and KEY not in caplog.text
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("key", ["", " ", None, "key\r\nInjected: x"])
def test_invalid_key_rejected(key):  # type: ignore[no-untyped-def]
    with pytest.raises(CustomAPIError):
        TonpayCustomClient(key)


def test_client_has_no_financial_or_registration_dependency():
    path = Path(__file__).parents[2] / "paurubot/payments/providers/tonpay_custom.py"
    tree = ast.parse(path.read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(
        name.startswith(("paurubot.db", "paurubot.ledger", "paurubot.payments.engine")) for name in imports
    )
    assert "GATEWAYS" not in path.read_text()


async def test_cancel_during_image_validation_never_uploads(monkeypatch):  # type: ignore[no-untyped-def]
    import asyncio
    import threading

    started = threading.Event()
    release = threading.Event()
    body = pixels()

    def validate(content):  # type: ignore[no-untyped-def]
        started.set()
        if not release.wait(5):
            raise AssertionError("validation test was not released")
        return receipt_image(content)

    monkeypatch.setattr("paurubot.payments.providers.tonpay_custom.receipt_image", validate)
    api, calls = client(invoice(receipt_received=True))
    task = asyncio.create_task(api.submit_receipt("invoice_42", "42", 100_000, body))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert not calls


@pytest.mark.parametrize("operation", ["create", "check", "change_card", "submit_receipt"])
@pytest.mark.parametrize("length", [20, 21])
async def test_documented_order_id_boundary(operation, length):  # type: ignore[no-untyped-def]
    order = "o" * length
    api, calls = client(invoice(order_id=order, receipt_received=True))

    async def invoke():  # type: ignore[no-untyped-def]
        if operation == "create":
            return await api.create(order, 100_000, 123, CALLBACK)
        if operation == "submit_receipt":
            return await api.submit_receipt("invoice_42", order, 100_000, pixels())
        return await getattr(api, operation)("invoice_42", order, 100_000)

    if length == 20:
        assert (await invoke()).order_id == order
        assert len(calls) == 1
    else:
        with pytest.raises(CustomAPIError) as exc:
            await invoke()
        assert exc.value.code == "invalid_identity"
        assert not calls
