"""A backup's header can't make the restore derive a huge scrypt key (audit SEC-07).

The archive header carries the scrypt cost. `n` was bounded but `r` and `p` (up to 255 each) were
taken as written, so a crafted file could make verify or restore allocate gigabytes. Only the
cost PauruBot writes is accepted now, and a refused header never reaches the key derivation.
"""

from __future__ import annotations

import io

import pytest

from paurubot.ops import crypto


def _archive(**override: int) -> bytes:
    out = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(b"payload"), out, "a-long-passphrase", n_log2=10)
    raw = bytearray(out.getvalue())
    for name, offset in (("r", 6), ("p", 7)):  # MAGIC(4) version(1) n_log2(1) r(1) p(1)
        if name in override:
            raw[offset] = override[name]
    return bytes(raw)


def test_the_written_cost_still_restores() -> None:
    out = io.BytesIO()
    crypto.decrypt_stream(io.BytesIO(_archive()), out, "a-long-passphrase")
    assert out.getvalue() == b"payload"


@pytest.mark.parametrize("field", [{"r": 255}, {"p": 255}, {"r": 0}, {"p": 0}])
def test_a_crafted_cost_is_refused_before_any_key_is_derived(field, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def never(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("scrypt ran on a refused header")

    crafted = _archive(**field)
    monkeypatch.setattr(crypto, "_key", never)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_stream(io.BytesIO(crafted), io.BytesIO(), "a-long-passphrase")
