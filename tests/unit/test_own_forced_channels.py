"""A user who runs a bot of their own (a dedicated own-panel bot, or a reseller's bot) sets the
channels its customers must join, like the main shop does (GRW-07). A channel is accepted only when
that bot is an admin there: otherwise the join check can never run and nobody would be held."""

from __future__ import annotations

from aiogram.types import Chat
from sqlalchemy import select

from paurubot.bot.context import missing_channels
from paurubot.core import forced_join
from paurubot.db.models import AuditLog, ForcedChannel
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import BOT_USER, chat_key
from tests.unit.test_own_bot_miniapp import BUYER_TG, OWNER_TG, own, signed  # noqa: F401  (fixture)


def _channel(tg, name: str, chat_id: int, *, admin: bool = True) -> None:  # type: ignore[no-untyped-def]
    chat = Chat(id=chat_id, type="channel", title=name.title(), username=name)
    tg.chats[chat_key(f"@{name}")] = chat
    tg.chats[str(chat_id)] = chat
    if admin:
        tg.admins.add((str(chat_id), BOT_USER.id))


async def test_the_owner_of_a_dedicated_bot_sets_its_forced_channels(session, own) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, h, tg, child = own["client"], signed(OWNER_TG), own["tg"], own["child"]
    _channel(tg, "ali_news", -1007000001)
    _channel(tg, "ali_chat", -1007000002, admin=False)

    empty = (await client.get("/api/webapp/own/channels", headers=h)).json()
    assert empty == {"channels": [], "limit": forced_join.MAX_CHANNELS}

    made = await client.post("/api/webapp/own/channels", headers=h, json={"channel": "https://t.me/ali_news"})
    assert made.status_code == 200, made.text
    row = made.json()["channel"]
    assert row["title"] == "Ali_News" and row["url"] == "https://t.me/ali_news" and row["is_active"] is True
    stored = await session.scalar(select(ForcedChannel).where(ForcedChannel.id == row["id"]))
    assert stored is not None and stored.shop_id == child.id and stored.chat_id == "-1007000001"

    again = await client.post("/api/webapp/own/channels", headers=h, json={"channel": "@ali_news"})
    assert again.status_code == 409 and again.json()["error"] == "channel_exists"
    not_admin = await client.post("/api/webapp/own/channels", headers=h, json={"channel": "@ali_chat"})
    assert not_admin.status_code == 422 and not_admin.json()["error"] == "bot_not_admin"
    unknown = await client.post("/api/webapp/own/channels", headers=h, json={"channel": "@nobody_here"})
    assert unknown.status_code == 422 and unknown.json()["error"] == "channel_not_found"

    paused = await client.put(f"/api/webapp/own/channels/{row['id']}", headers=h, json={"is_active": False})
    assert paused.status_code == 200 and paused.json()["channel"]["is_active"] is False
    await client.put(f"/api/webapp/own/channels/{row['id']}", headers=h, json={"is_active": True})

    # its buyers must now join before using the bot
    buyer = await factories.customer(session, child, telegram_id=BUYER_TG)
    await session.commit()
    tg.non_members.add(("-1007000001", BUYER_TG))
    assert [c.chat_id for c in await missing_channels(notify_telegram.get_bot(), session, buyer)] == [
        "-1007000001"
    ]

    actions = set(await session.scalars(select(AuditLog.action).where(AuditLog.shop_id == child.id)))
    assert {"own.channel.add", "own.channel.set"} <= actions
    assert (await client.delete(f"/api/webapp/own/channels/{row['id']}", headers=h)).json() == {
        "deleted": True
    }
    assert await session.scalar(select(ForcedChannel.id).where(ForcedChannel.id == row["id"])) is None


async def test_only_the_bots_owner_and_only_its_own_channels(session, own) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    client, tg = own["client"], own["tg"]
    _channel(tg, "ali_news", -1007000001)
    for method, path, body in (
        ("GET", "/api/webapp/own/channels", None),
        ("POST", "/api/webapp/own/channels", {"channel": "@ali_news"}),
    ):
        resp = await client.request(method, path, headers=signed(BUYER_TG), json=body)
        assert resp.status_code == 403
    main_row = ForcedChannel(
        shop_id=own["shop"].id, chat_id="-1009999", title="Main", url="https://t.me/main"
    )
    session.add(main_row)
    await session.commit()
    h = signed(OWNER_TG)
    assert (await client.delete(f"/api/webapp/own/channels/{main_row.id}", headers=h)).status_code == 404
    assert (
        await client.put(f"/api/webapp/own/channels/{main_row.id}", headers=h, json={"is_active": False})
    ).status_code == 404
    listed = (await client.get("/api/webapp/own/channels", headers=h)).json()["channels"]
    assert listed == [], "the main shop's channels are not this bot's"


async def test_a_bot_has_a_limited_number_of_forced_channels(session, own) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    for n in range(forced_join.MAX_CHANNELS):
        session.add(
            ForcedChannel(
                shop_id=own["child"].id, chat_id=f"-10070001{n}", title=f"c{n}", url="https://t.me/c"
            )
        )
    await session.commit()
    _channel(own["tg"], "ali_news", -1007000001)
    resp = await own["client"].post(
        "/api/webapp/own/channels", headers=signed(OWNER_TG), json={"channel": "@ali_news"}
    )
    assert resp.status_code == 422 and resp.json()["error"] == "channel_limit"


async def test_core_reads_a_username_a_link_or_the_old_pair(session, own) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    tg, child = own["tg"], own["child"]
    _channel(tg, "ali_news", -1007000001)
    _channel(tg, "ali_chat", -1007000002, admin=False)
    row = await forced_join.add(session, child.id, "@ali_news | https://t.me/+private_invite")
    assert row.url == "https://t.me/+private_invite", "an invite link given with the channel is kept"
    try:
        await forced_join.add(session, child.id, "ali_chat")
    except forced_join.ForcedJoinError as exc:
        assert exc.code == "bot_not_admin"
    else:
        raise AssertionError("a channel the bot cannot check is refused")
