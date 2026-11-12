"""Search boxes match the typed text literally (audit SEC-06).

`_` and `%` are wildcards in SQL LIKE. Passed through unescaped, a search for `a_b` also found `axb`,
and a search of `%` matched every row, which on a big shop means a full scan.
"""

from __future__ import annotations

from paurubot.core import customers, reseller_desk
from paurubot.db.models import Service, ServiceStatus
from paurubot.db.search import contains
from tests import factories


def test_the_pattern_escapes_wildcards() -> None:
    assert contains("a_b%c\\d") == "%a\\_b\\%c\\\\d%"


async def test_customer_search_treats_wildcards_as_text(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await factories.customer(session, shop, telegram_id=1001, username="ali_reza")
    await factories.customer(session, shop, telegram_id=1002, username="alixreza")
    await session.commit()

    found, total = await customers.search(session, shop.id, "ali_reza")
    assert [c.username for c in found] == ["ali_reza"] and total == 1
    assert (await customers.search(session, shop.id, "%"))[1] == 0, "a lone % matches nothing literal"


async def test_reseller_service_search_treats_wildcards_as_text(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    owner = await factories.customer(session, shop, telegram_id=2001)
    for name in ("sara_1", "saraX1"):
        session.add(
            Service(
                shop_id=shop.id,
                customer_id=owner.id,
                panel_id=panel.id,
                panel_username=name,
                label=name,
                status=ServiceStatus.ACTIVE,
            )
        )
    await session.commit()

    found = await reseller_desk.search(session, owner, "sara_1")
    assert [s.panel_username for s in found] == ["sara_1"]
