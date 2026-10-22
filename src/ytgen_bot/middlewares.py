"""Cross-cutting gates, outermost first: private chats only -> per-user throttle -> consent.

`GuardMiddleware` wraps EVERY handler; `ConsentMiddleware` wraps every handler except /start, /stop and the
consent button (those live in a router that does not use it).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import keyboards, texts
from .context import AppContext
from .ui import answer, fail, mark_blocked, say
from .wrapper import WRAPPER_FAILURES

log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 1000

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


def _user_and_chat(event: TelegramObject) -> tuple[int | None, str | None]:
    if isinstance(event, Message):
        return (event.from_user.id if event.from_user and not event.from_user.is_bot else None), event.chat.type
    if isinstance(event, CallbackQuery):
        chat = event.message.chat if event.message is not None else None
        return (event.from_user.id if not event.from_user.is_bot else None), (chat.type if chat else "private")
    return None, None


class GuardMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        uid, chat_type = _user_and_chat(event)
        if uid is None or chat_type != "private":
            log.debug("update_ignored", extra={"chat_type": chat_type})
            return None
        bot: Bot = data["bot"]

        decision = self._ctx.limiter.check(uid)
        if not decision.allowed:
            log.info("throttled", extra={"tg_user": uid, "retry_after_s": decision.retry_after_s})
            if isinstance(event, CallbackQuery):
                await answer(event, texts.too_fast(decision.retry_after_s))
            elif decision.notify:
                await say(self._ctx, bot, uid, texts.too_fast(decision.retry_after_s))
            return None

        if isinstance(event, Message) and event.text and len(event.text) > MAX_TEXT_LENGTH:
            await say(self._ctx, bot, uid, texts.TOO_LONG)
            return None

        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            await mark_blocked(self._ctx, uid)
            return None


class ConsentMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        uid, _ = _user_and_chat(event)
        bot: Bot = data["bot"]
        if uid is None:
            return None
        try:
            # Consent is read from the wrapper (GET /me: read-only, no row, no event), never remembered here,
            # so it survives a restart and an opt-out takes effect at once.
            consented = (await self._ctx.wrapper.me(uid)).consented
        except WRAPPER_FAILURES as exc:
            if isinstance(event, CallbackQuery):
                await answer(event)
            await fail(self._ctx, bot, uid, exc, "generic")
            return None
        if consented:
            return await handler(event, data)
        log.info("consent_required", extra={"tg_user": uid})
        if isinstance(event, CallbackQuery):
            await answer(event, texts.CONSENT_FIRST)
        await say(self._ctx, bot, uid, texts.consent_request(), keyboards.consent())
        return None
