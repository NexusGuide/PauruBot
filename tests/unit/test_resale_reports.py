"""Profit summaries and Excel exports for reseller bots."""

from __future__ import annotations

import io
from zipfile import ZipFile

from openpyxl import load_workbook

from paurubot.core import resale, resale_reports
from paurubot.db.base import utcnow
from paurubot.db.models import Order, OrderKind, OrderStatus
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


async def test_trials_and_moves_are_not_sales(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    buyer, _order = await _sell(session, child, env["plan"], 63211, 96_000)
    session.add_all(
        [
            Order(
                shop_id=child.id,
                customer_id=buyer.id,
                kind=OrderKind.MOVE,
                status=OrderStatus.COMPLETED,
                amount=50_000,
                completed_at=utcnow(),
            ),
            Order(
                shop_id=child.id,
                customer_id=buyer.id,
                kind=OrderKind.TRIAL,
                status=OrderStatus.COMPLETED,
                amount=0,
                completed_at=utcnow(),
            ),
        ]
    )
    await session.commit()
    report = await resale_reports.summary(session, child.id, "month")
    assert (report.sales, report.revenue, report.wholesale, report.profit) == (1, 96_000, 80_000, 16_000)


async def test_customer_text_cannot_open_a_formula_in_excel(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    buyer, _order = await _sell(session, child, env["plan"], 63212, 96_000)
    payload = '=HYPERLINK("http://example.invalid","click")'
    buyer.username = None
    buyer.first_name = payload
    await session.commit()
    data = await resale_reports.workbook(session, child.id, "month")
    with ZipFile(io.BytesIO(data)) as archive:
        orders_xml = archive.read("xl/worksheets/sheet2.xml").decode()
    assert "<f>" not in orders_xml
    loaded = load_workbook(io.BytesIO(data))["orders"]["D2"]
    assert loaded.value == payload and loaded.data_type == "s"
