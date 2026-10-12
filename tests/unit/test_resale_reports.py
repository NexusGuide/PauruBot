"""Profit summaries and Excel exports for reseller bots."""

from __future__ import annotations

from paurubot.core import resale, resale_reports
from tests.unit.test_reseller_bot import TOKEN, _sell
from tests.unit.test_reseller_bot import env as env  # noqa: F401


async def test_reseller_profit_summary_and_excel(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    await _sell(session, child, env["plan"], 63201, 96_000)
    report = await resale_reports.summary(session, child.id, "month")
    assert report.sales == 1
    assert report.revenue == 96_000
    assert report.wholesale == 80_000
    assert report.profit == 16_000
    data = await resale_reports.workbook(session, child.id, "month")
    assert data[:2] == b"PK"
