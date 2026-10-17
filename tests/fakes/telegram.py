"""A recording stand-in for the Telegram Bot API, so dispatcher flows run without the network."""

from __future__ import annotations

import io
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    CopyMessage,
    DeleteWebhook,
    EditMessageCaption,
    EditMessageText,
    GetChat,
    GetChatMember,
    GetFile,
    GetMe,
    SendInvoice,
    SendMessage,
    SendPhoto,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    File,
    Message,
    MessageId,
    PhotoSize,
    Update,
    User,
)

TOKEN = "123456:TEST-token-for-unit-tests-only-xxxxxxxx"
BOT_USER = User(id=123456, is_bot=True, first_name="Pauru", username="pauru_test_bot")


def chat_key(value: Any) -> str:
    """`@name` and `name` are the same chat, and so are the int and string forms of an id."""
    text = str(value).strip()
    return text[1:] if text.startswith("@") else text


_ids = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.non_members: set[tuple[str, int]] = set()
        self.admins: set[tuple[str, int]] = set()
        self.chats: dict[str, Chat] = {}
        self.unreachable: set[str] = set()
        self.files: dict[str, bytes] = {}
        self.blocked: set[int] = set()

    async def close(self) -> None:
        return None

    def _message(self, chat_id: int, **fields: Any) -> Message:
        return Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=BOT_USER,
            **fields,
        )

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        chat_id = getattr(method, "chat_id", None)
        if isinstance(chat_id, int) and chat_id in self.blocked:
            from aiogram.exceptions import TelegramForbiddenError

            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, GetChat):
            from aiogram.exceptions import TelegramBadRequest

            chat = self.chats.get(chat_key(method.chat_id))
            if chat is None:
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            return chat
        if isinstance(method, SendMessage):
            return self._message(int(method.chat_id), text=method.text)
        if isinstance(method, SendPhoto):
            return self._message(
                int(method.chat_id),
                caption=method.caption,
                photo=[PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)],
            )
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            return self._message(int(method.chat_id or 0), text=getattr(method, "text", None) or "x")
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(_ids))
        if isinstance(method, GetChatMember):
            from aiogram.exceptions import TelegramBadRequest

            if chat_key(method.chat_id) in self.unreachable:
                raise TelegramBadRequest(method=method, message="Bad Request: bot is not a member")
            who = User(id=method.user_id, is_bot=method.user_id == BOT_USER.id, first_name="x")
            if (str(method.chat_id), method.user_id) in self.non_members:
                return ChatMemberLeft(user=who)
            if (str(method.chat_id), method.user_id) in self.admins:
                return ChatMemberAdministrator.model_construct(
                    user=who, status="administrator", is_member=True
                )
            return ChatMemberMember(user=who)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=method.file_id)
        if isinstance(method, (AnswerCallbackQuery, AnswerPreCheckoutQuery, SetMyCommands, DeleteWebhook)):
            return True
        if isinstance(method, SendInvoice):
            return self._message(int(method.chat_id), text="invoice")
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        file_id = url.rsplit("/", 1)[-1]
        yield self.files.get(file_id, b"")

    # --- helpers for assertions ----------------------------------------------------

    def texts(self, chat_id: int | None = None) -> list[str]:
        out = []
        for call in self.calls:
            text = getattr(call, "text", None) or getattr(call, "caption", None)
            if text and (chat_id is None or getattr(call, "chat_id", None) == chat_id):
                out.append(text)
        return out

    def last(self, kind: type) -> Any:
        for call in reversed(self.calls):
            if isinstance(call, kind):
                return call
        raise AssertionError(f"no {kind.__name__} call")

    def buttons(self, call: Any) -> list[tuple[str, str | None]]:
        markup = getattr(call, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or []
        return [(b.text, b.callback_data) for row in rows for b in row]

    def reset(self) -> None:
        self.calls.clear()


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


def user(uid: int, name: str = "Ali", username: str | None = "ali", lang: str = "fa") -> User:
    return User(id=uid, is_bot=False, first_name=name, username=username, language_code=lang)


def message_update(uid: int, text: str | None = None, **fields: Any) -> Update:
    from_user = fields.pop("from_user", None) or user(uid)
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=from_user,
        text=text,
        **fields,
    )
    return Update(update_id=next(_ids), message=msg)


def photo_update(uid: int, file_id: str, caption: str | None = None) -> Update:
    return message_update(
        uid,
        None,
        caption=caption,
        photo=[PhotoSize(file_id=file_id, file_unique_id=file_id, width=100, height=100)],
    )


def callback_update(uid: int, data: str) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=BOT_USER,
        text="menu",
    )
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=user(uid), chat_instance="ci", message=msg, data=data
        ),
    )


def png_bytes() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (120, 80), "white")
    ImageDraw.Draw(img).rectangle([10, 10, 60, 50], fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
