from __future__ import annotations

import ast
import inspect
from typing import Any

from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_tools
from paurubot.db.models import ResellerProfile
from tests import factories
from tests.bot.test_customer_controls import OWNER, UID
from tests.bot.test_customer_controls import env as env  # noqa: F401
from tests.fakes.telegram import callback_update, message_update


async def test_owner_can_set_and_clear_daily_sales_cap(session: Any, env: Any) -> None:
    shop, send = env["shop"], env["send"]
    customer = await factories.customer(session, shop, telegram_id=UID)
    profile = ResellerProfile(shop_id=shop.id, customer_id=customer.id, permissions=[])
    session.add(profile)
    await session.commit()
    await send(message_update(OWNER, "/start"))
    for raw, expected in [("12", 12), ("0", None)]:
        await send(callback_update(OWNER, Admin(section="ulimit", id=customer.id).pack()))
        await send(message_update(OWNER, raw))
        await session.refresh(profile)
        assert profile.max_purchases_per_day == expected


def test_daily_cap_save_requires_management_permission() -> None:
    node = ast.parse(inspect.getsource(admin_tools.reseller_limit_save)).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    decorator = ast.unparse(node.decorator_list[0])
    assert "StaffOnly('resellers.manage')" in decorator
