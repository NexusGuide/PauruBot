"""Fill a development database with believable demo data so the admin panel has something to show.

Usage: python scripts/seed_demo.py          (refuses to run when ENVIRONMENT=production)
"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from paurubot.config import get_settings  # noqa: E402
from paurubot.core import customers  # noqa: E402
from paurubot.db.models import (  # noqa: E402
    Category,
    Location,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    PanelKind,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Plan,
    ResellerApplication,
    ResellerGroup,
    Service,
    ServiceStatus,
    Shop,
    Ticket,
    TicketMessage,
)
from paurubot.db.session import dispose_engine, session_scope  # noqa: E402
from paurubot.ledger import wallet  # noqa: E402
from paurubot.panels import GB  # noqa: E402
from paurubot.security.secretbox import encrypt  # noqa: E402

NAMES = [
    "ali_r",
    "sara.m",
    "reza77",
    "mina_k",
    "hamid",
    "nazanin",
    "omid_dev",
    "leila",
    "pouya",
    "shirin",
    "arash",
    "yasaman",
]


async def main() -> int:
    if get_settings().is_production:
        print("refusing to seed a production database")
        return 1
    rnd = random.Random(7)
    now = datetime.now(UTC)
    async with session_scope() as session:
        shop = await session.scalar(select(Shop).order_by(Shop.id))
        if shop is None:
            print("complete the setup wizard first")
            return 1
        de = Location(shop_id=shop.id, title="Germany", flag="🇩🇪", sort=1)
        nl = Location(shop_id=shop.id, title="Netherlands", flag="🇳🇱", sort=2)
        monthly = Category(shop_id=shop.id, title="Monthly", sort=1)
        session.add_all([de, nl, monthly])
        await session.flush()
        panels = [
            Panel(
                shop_id=shop.id,
                name="DE-1",
                kind=PanelKind.PASARGUARD,
                base_url="https://de1.example.com",
                username="admin",
                password_enc=encrypt("panel", "demo"),
                location_id=de.id,
                capacity=500,
                is_healthy=True,
                health_checked_at=now - timedelta(minutes=4),
                options={"group_ids": [1]},
            ),
            Panel(
                shop_id=shop.id,
                name="NL-1",
                kind=PanelKind.XUI,
                base_url="https://nl1.example.com:2053",
                username="admin",
                password_enc=encrypt("panel", "demo"),
                location_id=nl.id,
                is_healthy=False,
                health_checked_at=now - timedelta(minutes=9),
                health_error="connection failed: ConnectTimeout",
                options={"inbound_id": 2},
            ),
        ]
        session.add_all(panels)
        await session.flush()
        plans = [
            Plan(
                shop_id=shop.id,
                panel_id=panels[0].id,
                category_id=monthly.id,
                title="30 days · 50 GB",
                price=120_000,
                days=30,
                data_gb=50,
                sort=1,
            ),
            Plan(
                shop_id=shop.id,
                panel_id=panels[0].id,
                category_id=monthly.id,
                title="30 days · 100 GB",
                price=190_000,
                days=30,
                data_gb=100,
                sort=2,
            ),
            Plan(
                shop_id=shop.id,
                location_id=nl.id,
                category_id=monthly.id,
                title="90 days · 300 GB",
                price=480_000,
                days=90,
                data_gb=300,
                sort=3,
            ),
        ]
        session.add_all(plans)
        session.add(ResellerGroup(shop_id=shop.id, title="Silver", discount_percent=15, min_topup=500_000))
        await session.flush()

        people = []
        for index, name in enumerate(NAMES):
            row, _ = await customers.upsert_from_telegram(
                session, shop.id, 7_000_000 + index, username=name, first_name=name.title()
            )
            row.created_at = now - timedelta(days=rnd.randint(0, 40))
            people.append(row)
        await session.flush()

        statuses = [ServiceStatus.ACTIVE] * 6 + [
            ServiceStatus.LIMITED,
            ServiceStatus.EXPIRED,
            ServiceStatus.DISABLED,
        ]
        for number, person in enumerate(people):
            await wallet.topup(
                session,
                shop.id,
                person.id,
                rnd.choice([0, 50_000, 200_000]) or 10_000,
                provider="card",
                payment_id=900_000 + number,
            )
            plan = rnd.choice(plans)
            limit = plan.data_gb * GB
            status = rnd.choice(statuses)
            used = limit if status == ServiceStatus.LIMITED else int(limit * rnd.random() * 0.95)
            expire = (
                now + timedelta(days=rnd.randint(1, 29))
                if status != ServiceStatus.EXPIRED
                else now - timedelta(days=2)
            )
            service = Service(
                shop_id=shop.id,
                customer_id=person.id,
                panel_id=panels[number % 2].id,
                plan_id=plan.id,
                panel_username=f"{person.username}_{number}",
                status=status,
                data_limit=limit,
                used_bytes=used,
                expire_at=expire,
                synced_at=now - timedelta(minutes=rnd.randint(1, 30)),
                subscription_url=f"https://de1.example.com/sub/{person.username}{number}",
            )
            session.add(service)
        for day in range(30):
            for _ in range(rnd.randint(0, 6)):
                plan = rnd.choice(plans)
                done = now - timedelta(days=day, hours=rnd.randint(0, 20))
                session.add(
                    Order(
                        shop_id=shop.id,
                        customer_id=rnd.choice(people).id,
                        kind=rnd.choice([OrderKind.PURCHASE, OrderKind.RENEW]),
                        status=OrderStatus.COMPLETED,
                        plan_id=plan.id,
                        base_price=plan.price,
                        amount=plan.price,
                        paid_at=done,
                        completed_at=done,
                        created_at=done,
                    )
                )
        session.add(
            Order(
                shop_id=shop.id,
                customer_id=people[3].id,
                kind=OrderKind.PURCHASE,
                status=OrderStatus.FAILED,
                plan_id=plans[2].id,
                base_price=plans[2].price,
                amount=plans[2].price,
                error="panel rejected request: no free capacity",
            )
        )
        for number, person in enumerate(people[:3]):
            session.add(
                Payment(
                    shop_id=shop.id,
                    customer_id=person.id,
                    purpose=PaymentPurpose.TOPUP,
                    provider="card",
                    status=PaymentStatus.VERIFYING,
                    amount=[200_000, 500_000, 120_000][number],
                    idempotency_key=f"demo:receipt:{number}",
                    receipt_text="Transfer from Mellat, ref 4821",
                )
            )
        ticket = Ticket(shop_id=shop.id, customer_id=people[5].id, subject="Connection drops on mobile data")
        session.add(ticket)
        await session.flush()
        session.add(
            TicketMessage(
                ticket_id=ticket.id,
                author_type="customer",
                author_id=people[5].id,
                text="It works on wifi but drops every few minutes on mobile data.",
            )
        )
        session.add(
            ResellerApplication(
                shop_id=shop.id, customer_id=people[6].id, text="I sell to about 40 people at my office."
            )
        )
    await dispose_engine()
    print("demo data added")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
