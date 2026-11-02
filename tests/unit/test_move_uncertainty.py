"""SEC-F08 for moves: a move whose new account may exist is parked, never refunded blindly.

A move creates the service's account on the target panel. When that create timed out and the
job then ran out of retries, the move fee was refunded and the claim released although the
target panel might hold the new account. Now the move records its create intent like a purchase,
gives up by parking, and reconciliation reads the target panel: an existing account finishes
the move, a missing one refunds it.
"""

from __future__ import annotations

from paurubot.core import settings as shop_settings
from paurubot.db.models import Order, OrderKind, OrderStatus, Service
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import registry
from paurubot.provisioning import create_state, lifecycle
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests.fakes.panels import transport
from tests.unit import test_service_move
from tests.unit.test_create_uncertainty import _create_then_lose, _down, _exhaust, _settled, _use, drain
from tests.unit.test_service_lifecycle import _buy, _service

two = test_service_move.two
isolate_queue = test_service_move.isolate_queue


async def _move_lost(session, two, *, created: bool):  # type: ignore[no-untyped-def]
    cust, new = two["cust"], two["new"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    await shop_settings.set_many(session, two["shop"].id, {"move_price": 15_000})
    order = await _buy(session, cust, OrderKind.MOVE, service=service, move_to=new)
    after_charge = await wallet_balance(session, two["shop"].id, cust.id)
    fake = two["fakes"][new.id]
    _use(new, _create_then_lose(fake, created))
    claimed = await queue.claim(session, "provision", limit=1)
    await run_job("provision", QUEUES["provision"], claimed[0])
    await session.refresh(order)
    assert create_state.state(order) == create_state.ATTEMPTED
    _use(new, _down)  # the target panel stays down until the job gives up
    await _exhaust(session, order.id)
    order = await session.get(Order, order.id, populate_existing=True)
    assert order is not None and order.status == OrderStatus.PROVISIONING and create_state.parked(order)
    assert await wallet_balance(session, two["shop"].id, cust.id) == after_charge  # nothing refunded
    registry.test_transports[new.id] = transport(fake)  # the panel is back
    registry._cache.clear()
    await _settled(session, order)
    return service, order, fake, after_charge


async def test_a_new_account_that_exists_finishes_the_move(session, two) -> None:  # type: ignore[no-untyped-def]
    service, order, fake, after_charge = await _move_lost(session, two, created=True)
    assert len(fake.users) == 1
    assert await lifecycle.reconcile_purchase(session, order.id) == "resumed"
    await drain(session)
    moved = await session.get(Service, service.id, populate_existing=True)
    assert moved is not None and moved.panel_id == two["new"].id
    assert len(fake.users) == 1 and moved.panel_username in fake.users  # adopted, not created twice
    order = await session.get(Order, order.id, populate_existing=True)
    assert order.status == OrderStatus.COMPLETED and create_state.state(order) == create_state.CONFIRMED
    assert await wallet_balance(session, two["shop"].id, two["cust"].id) == after_charge


async def test_a_new_account_that_is_missing_refunds_the_move(session, two) -> None:  # type: ignore[no-untyped-def]
    service, order, fake, after_charge = await _move_lost(session, two, created=False)
    assert fake.users == {}
    assert await lifecycle.reconcile_purchase(session, order.id) == "refunded"
    stayed = await session.get(Service, service.id, populate_existing=True)
    assert stayed is not None and stayed.panel_id == two["old"].id  # still where it was
    order = await session.get(Order, order.id, populate_existing=True)
    assert order.status == OrderStatus.REFUNDED
    assert await wallet_balance(session, two["shop"].id, two["cust"].id) == after_charge + 15_000
