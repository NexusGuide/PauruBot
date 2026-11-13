"""A burst of updates waits for a Redis connection instead of failing (MaxConnectionsError).

redis-py's pool raised "Too many connections" the moment its 100 connections were in use, and
the bot handled every polled update at once with no bound. Several bots polling together could
fill the bot's FSM pool, and updates then failed instead of waiting a few milliseconds.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import BlockingConnectionPool

from paurubot import kv
from paurubot.bot import app
from paurubot.config import get_settings


def test_the_shared_client_waits_for_a_free_connection() -> None:
    client = kv.make_client("redis://localhost:6379/0")
    pool = client.connection_pool
    assert isinstance(pool, BlockingConnectionPool)
    assert pool.max_connections == get_settings().redis_max_connections
    assert pool.timeout == get_settings().redis_pool_timeout


def test_the_bot_state_storage_waits_for_a_free_connection() -> None:
    storage = app.fsm_storage()
    assert isinstance(storage.redis.connection_pool, BlockingConnectionPool)


async def test_a_full_pool_waits_instead_of_failing() -> None:
    client = kv.make_client("redis://localhost:6379/0", max_connections=1)
    try:
        held = await client.connection_pool.get_connection()
        later = asyncio.create_task(client.connection_pool.get_connection())
        await asyncio.sleep(0.05)
        assert not later.done(), "the second caller waits for the connection"
        await client.connection_pool.release(held)
        second = await asyncio.wait_for(later, 2)
        await client.connection_pool.release(second)
    finally:
        await client.aclose()


def test_polling_handles_a_bounded_number_of_updates_at_once() -> None:
    from paurubot.cli import commands

    options = commands.polling_options()
    assert options["handle_as_tasks"] is True
    assert options["tasks_concurrency_limit"] == get_settings().bot_update_concurrency
    assert 0 < get_settings().bot_update_concurrency < get_settings().redis_max_connections
