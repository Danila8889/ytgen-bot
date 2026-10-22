"""Sending messages and answering callbacks, with the Telegram failure modes handled in one place."""
from __future__ import annotations

import logging
from collections.abc import Mapping

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .context import AppContext
from .errors_ru import ErrorView, describe
from .wrapper import WrapperError

log = logging.getLogger(__name__)


async def mark_blocked(ctx: AppContext, uid: int) -> None:
    """Telegram refused delivery (the user blocked the bot, or the account is gone): tell the wrapper."""
    log.warning("telegram_forbidden", extra={"tg_user": uid})
    try:
        await ctx.wrapper.set_blocked(uid, True)
    except Exception:
        log.exception("blocked_report_failed", extra={"tg_user": uid})


async def say(
    ctx: AppContext,
    bot: Bot,
    uid: int,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Send a message to a private chat. Returns None when it could not be delivered (already reported)."""
    try:
        return await bot.send_message(uid, text, reply_markup=kb)
    except TelegramForbiddenError:
        await mark_blocked(ctx, uid)
        return None
    except TelegramAPIError:
        log.exception("telegram_send_failed", extra={"tg_user": uid})
        return None


async def answer(callback: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    """Acknowledge a button press (stops the client spinner). Never raises."""
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramAPIError:
        # Typically "query is too old": the press is stale, nothing else to do, but it is on record.
        log.warning("callback_answer_failed", extra={"tg_user": callback.from_user.id})


async def fail(
    ctx: AppContext,
    bot: Bot,
    uid: int,
    exc: Exception,
    call_ctx: str,
    *,
    kbs: Mapping[str, InlineKeyboardMarkup] | None = None,
) -> ErrorView:
    """Show the mapped message for a failed wrapper call, with the keyboard chosen by the error's
    follow-up (`kbs[view.next]`), and log it with its context."""
    view = describe(exc, call_ctx, ctx.support)
    if isinstance(exc, WrapperError):
        log.info(
            "user_facing_wrapper_error",
            extra={"tg_user": uid, "ctx": call_ctx, "code": exc.code, "status": exc.status},
        )
    await say(ctx, bot, uid, view.text, (kbs or {}).get(view.next))
    return view
