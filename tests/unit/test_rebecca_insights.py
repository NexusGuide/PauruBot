"""AUD-6: Rebecca's address list is read once for many services, not once per service.

Rebecca only offers every user's live addresses at once (/api/core/access/insights). Each
service's device check read the whole list again: a device-limit pass over 60 services was 60
full downloads, and the Mini App's "devices now" read did the same per customer.
"""

from __future__ import annotations

import asyncio

from paurubot.panels.base import PanelRef
from paurubot.panels.drivers import rebecca
from tests.contract.test_drivers import make


def _reads(fake) -> int:  # type: ignore[no-untyped-def]
    return int(fake.insight_reads)


async def test_one_read_serves_every_service_for_a_few_seconds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    driver, fake = make("rebecca")
    fake.sources = {f"u{i}": [f"10.0.0.{i}"] for i in range(30)}
    now = [1000.0]
    monkeypatch.setattr(rebecca.time, "monotonic", lambda: now[0])
    for i in range(30):
        assert await driver.client_ips(PanelRef(f"u{i}")) == [f"10.0.0.{i}"]
    assert _reads(fake) == 1
    # Readers at the same moment wait on the one read in flight.
    await asyncio.gather(*(driver.client_ips(PanelRef(f"u{i}")) for i in range(10)))
    assert _reads(fake) == 1
    # Past the window the list is read again, so a new device shows up.
    fake.sources["u1"] = ["10.0.0.1", "10.9.9.9"]
    now[0] += rebecca.INSIGHTS_TTL + 0.1
    assert await driver.client_ips(PanelRef("u1")) == ["10.0.0.1", "10.9.9.9"]
    assert _reads(fake) == 2
    assert await driver.client_ips(PanelRef("nobody")) == []
