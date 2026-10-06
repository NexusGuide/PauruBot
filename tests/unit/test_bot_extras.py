"""Menu layout and styles, help topics, app downloads, customer risk and staff-change notices."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.bot import menu
from paurubot.config import get_settings
from paurubot.core import apps, trials
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, OrderKind, Service
from paurubot.i18n import Texts
from tests import factories
from tests.unit.test_provisioning import _buy, _setup, drain


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
def fakes():  # type: ignore[no-untyped-def]
    from paurubot.panels import registry

    yield {}
    registry.test_transports.clear()
    registry._cache.clear()


def test_menu_layout_and_styles(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com")
    t = Texts("en")
    cfg = {
        "trial_enabled": False,
        "referral_enabled": True,
        "download_apps": [],
        "menu_layout": menu.validate_layout([["services", "buy"], ["trial", "wallet"], ["miniapp"]]),
        "button_styles": menu.validate_styles(
            {"buy": {"style": "success", "emoji_id": "5368324170671202286"}}
        ),
    }
    kb = menu.main_menu(t, cfg, is_staff=True)
    rows = [[b.text for b in row] for row in kb.keyboard]
    assert rows == [
        [t("btn_services"), t("btn_buy")],
        [t("btn_wallet")],
        [t("btn_miniapp")],
        [t("btn_admin")],
    ]
    buy = kb.keyboard[0][1]
    assert buy.style == "success" and buy.icon_custom_emoji_id == "5368324170671202286"
    assert kb.keyboard[2][0].web_app is not None
    default = menu.main_menu(t, {**cfg, "menu_layout": []}, is_staff=False)
    assert [b.text for b in default.keyboard[1]] == [t("btn_buy"), t("btn_services")]

    for bad in (
        [["buy", "buy"]],
        [["nope"]],
        [["wallet"]],
        "x",
        [["buy", "services", "wallet", "guide", "trial"]],
    ):
        with pytest.raises(menu.LayoutError):
            menu.validate_layout(bad)
    for bad in ({"buy": {"style": "rainbow"}}, {"buy": {"emoji_id": "abc"}}, {"ghost": {}}):
        with pytest.raises(menu.LayoutError):
            menu.validate_styles(bad)
    assert menu.validate_help([{"title": "Setup", "text": "Install the app"}])[0]["url"] == ""
    with pytest.raises(menu.LayoutError):
        menu.validate_help([{"title": "x", "url": "http://insecure.example"}])


async def test_app_release_lookup() -> None:
    app = apps.validate(
        [{"name": "v2rayNG", "platform": "android", "repo": "2dust/v2rayNG", "pattern": "*universal.apk"}]
    )[0]
    with pytest.raises(apps.AppError):
        apps.validate([{"name": "x", "platform": "android", "repo": "not a repo"}])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/2dust/v2rayNG/releases/latest"
        return httpx.Response(
            200,
            json={
                "tag_name": "1.9.30",
                "assets": [
                    {
                        "name": "v2rayNG_1.9.30_arm64-v8a.apk",
                        "browser_download_url": "https://x/a.apk",
                        "size": 10,
                    },
                    {
                        "name": "v2rayNG_1.9.30_universal.apk",
                        "browser_download_url": "https://x/u.apk",
                        "size": 20,
                    },
                ],
            },
        )

    shop_id = 991_001
    assert await apps.refresh(shop_id, [app], None, transport=httpx.MockTransport(handler)) == 1
    entry = await apps.cached(shop_id, app)
    assert entry == {
        "version": "1.9.30",
        "asset": "v2rayNG_1.9.30_universal.apk",
        "url": "https://x/u.apk",
        "size": 20,
        "file_id": None,
    }
    assert json.loads(json.dumps(entry))  # stored as plain JSON


async def test_risky_customers_get_no_trial_and_no_auto_approval(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Payment, PaymentPurpose, PaymentStatus
    from paurubot.payments import engine

    shop = await factories.shop(session)
    await factories.panel(session, shop)
    cust = await factories.customer(session, shop)
    assert await trials.eligibility(session, cust) is None
    cust.risk = "high"
    assert await trials.eligibility(session, cust) == "trial_unavailable"
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_auto_approve": True, "card_auto_max_amount": 1_000_000, "card_auto_min_history": 0},
    )
    payment = Payment(
        shop_id=shop.id,
        customer_id=cust.id,
        provider="card",
        purpose=PaymentPurpose.TOPUP,
        status=PaymentStatus.VERIFYING,
        amount=50_000,
        receipt_file_id="file",
        idempotency_key="risk-test-payment",
    )
    session.add(payment)
    await session.flush()
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    assert not await engine.auto_approvable(session, payment, cfg)
    cust.risk = "none"
    assert await engine.auto_approvable(session, payment, cfg)


async def test_staff_delete_tells_the_customer_why(session, fakes) -> None:  # type: ignore[no-untyped-def]
    from paurubot.worker import queue

    shop, panel, plan, cust, _ = await _setup(session, fakes)
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    service = await session.scalar(select(Service).where(Service.customer_id == cust.id))
    assert service is not None
    await queue.enqueue(
        session,
        "bulk",
        "service",
        {"service_id": service.id, "action": "delete", "reason": "abuse report", "notify": True},
        shop_id=shop.id,
    )
    await session.commit()
    await drain(session, "bulk")
    alert = await session.scalar(
        select(Job).where(Job.kind == "service_alert", Job.status == JobStatus.QUEUED).order_by(Job.id.desc())
    )
    assert alert is not None
    assert {k: v for k, v in alert.payload.items() if k != "_cid"} == {
        "service_id": service.id,
        "alert": "removed_by_staff",
        "reason": "abuse report",
    }


async def test_excel_export_and_top_customers(session, fakes) -> None:  # type: ignore[no-untyped-def]
    import io
    from datetime import timedelta

    from openpyxl import load_workbook

    from paurubot.core import exports
    from paurubot.db.base import utcnow

    shop, panel, plan, cust, _ = await _setup(session, fakes)
    cust.first_name = "=HYPERLINK(1)"
    cust.username = None
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    other = await factories.customer(session, shop)
    content = await exports.build(session, "orders", shop.id, start=None, end=None, customer_ids=None)
    sheet = load_workbook(io.BytesIO(content)).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:3] == ("id", "created_at", "completed_at")
    assert len(rows) == 3 and rows[1][4] == "'=HYPERLINK(1)"  # formulas are neutralised
    scoped = await exports.build(session, "orders", shop.id, start=None, end=None, customer_ids=[other.id])
    assert len(list(load_workbook(io.BytesIO(scoped)).active.iter_rows())) == 1
    top = await exports.top_customers(
        session, shop.id, start=utcnow() - timedelta(days=1), end=utcnow() + timedelta(minutes=1)
    )
    assert top[0]["customer_id"] == cust.id and top[0]["orders"] == 2 and top[0]["spent"] == 2 * plan.price
