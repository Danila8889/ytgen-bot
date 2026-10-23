"""aiogram without a network: a session that records every Bot API call and answers with canned results."""
from __future__ import annotations

import datetime as dt
from collections import deque
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteWebhook,
    GetMe,
    SendMessage,
    SetMyCommands,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

BOT_ID = 4242
BOT_USERNAME = "ytgen_test_bot"
BOT_TOKEN = "4242:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN"


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.errors: deque[Exception] = deque()  # raised by the next SendMessage
        self.get_me_errors: deque[Exception] = deque()  # raised by the next GetMe calls, one per call
        self.timeouts: list[int | None] = []  # the per-request timeout each call was made with

    async def close(self) -> None:  # pragma: no cover
        return None

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.requests.append(method)
        self.timeouts.append(timeout)
        if isinstance(method, SendMessage):
            if self.errors:
                raise self.errors.popleft()
            return Message(
                message_id=len(self.requests), date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
            )
        if isinstance(method, GetMe):
            if self.get_me_errors:
                raise self.get_me_errors.popleft()
            return User(id=BOT_ID, is_bot=True, first_name="YT", username=BOT_USERNAME)
        if isinstance(method, (AnswerCallbackQuery, SetMyCommands, DeleteWebhook)):
            return True
        raise AssertionError(f"unexpected Bot API call {type(method).__name__}")

    # --- inspection -----------------------------------------------------------------------

    def sent(self, uid: int | None = None) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage) and (uid is None or r.chat_id == uid)]

    def texts(self, uid: int | None = None) -> list[str]:
        return [m.text for m in self.sent(uid)]

    def last_text(self, uid: int | None = None) -> str:
        return self.texts(uid)[-1]

    def last_markup(self, uid: int | None = None) -> Any:
        return self.sent(uid)[-1].reply_markup

    def answers(self) -> list[AnswerCallbackQuery]:
        return [r for r in self.requests if isinstance(r, AnswerCallbackQuery)]

    def clear(self) -> None:
        self.requests.clear()


def buttons(markup: Any) -> list[Any]:
    return [b for row in (markup.inline_keyboard if markup else []) for b in row]


def message_update(
    uid: int, text: str, update_id: int = 1, *, chat_type: str = "private", language: str = "ru",
    username: str | None = None, first_name: str = "T", chat_id: int | None = None,
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id, date=dt.datetime.now(dt.UTC),
            chat=Chat(id=uid if chat_id is None else chat_id, type=chat_type),
            from_user=User(id=uid, is_bot=False, first_name=first_name, language_code=language, username=username),
            text=text,
        ),
    )


def callback_update(
    uid: int, data: str, update_id: int = 1, *, username: str | None = None, first_name: str = "T"
) -> Update:
    user = User(id=uid, is_bot=False, first_name=first_name, language_code="ru", username=username)
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id), from_user=user, chat_instance="ci", data=data,
            message=Message(
                message_id=1, date=dt.datetime.now(dt.UTC), chat=Chat(id=uid, type="private"),
                from_user=User(id=BOT_ID, is_bot=True, first_name="YT"), text="x",
            ),
        ),
    )
