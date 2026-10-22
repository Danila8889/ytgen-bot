"""Commands and menu buttons that only read: /help, /status, the main-menu targets, and the fallbacks."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, keyboards, texts
from ..context import AppContext
from ..ui import answer, fail, say
from ..wrapper import WRAPPER_FAILURES
from .filters import IsAction
from .link import begin_link
from .referral import show_referral
from .tariffs import show_tariffs

log = logging.getLogger(__name__)



async def show_status(ctx: AppContext, bot: Bot, uid: int) -> None:
    try:
        me = await ctx.wrapper.me(uid)
    except WRAPPER_FAILURES as exc:
        await fail(ctx, bot, uid, exc, "me")
        return
    if me.linked and me.tenant is not None:
        await say(ctx, bot, uid, texts.status_linked(me.tenant), keyboards.status_actions(True))
    else:
        await say(ctx, bot, uid, texts.status_unlinked(), keyboards.status_actions(False))


async def show_help(ctx: AppContext, bot: Bot, uid: int) -> None:
    await say(ctx, bot, uid, texts.help_text(ctx.support), keyboards.main_menu())


async def cmd_help(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_help(ctx, bot, message.from_user.id)


async def cmd_status(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_status(ctx, bot, message.from_user.id)


async def on_menu(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    uid = callback.from_user.id
    await answer(callback)
    await state.clear()
    target = action.args[0]
    if target == "tariffs":
        await show_tariffs(ctx, bot, uid)
    elif target == "referral":
        await show_referral(ctx, bot, uid)
    elif target == "status":
        await show_status(ctx, bot, uid)
    elif target == "help":
        await show_help(ctx, bot, uid)
    elif target == "link":
        await begin_link(ctx, bot, uid, state)


# Registered last in the gated router: whatever nothing above wanted.
async def on_stale_callback(callback: CallbackQuery, ctx: AppContext) -> None:
    log.warning("callback_unrecognised", extra={"tg_user": callback.from_user.id})
    await answer(callback, texts.STALE_BUTTON, alert=True)


async def on_unknown_message(message: Message, bot: Bot, ctx: AppContext) -> None:
    await say(ctx, bot, message.from_user.id, texts.UNKNOWN_MESSAGE, keyboards.main_menu())


def build_router() -> Router:
    router = Router(name="menu")
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_status, Command("status", "me"))
    router.callback_query.register(on_menu, IsAction("menu"))
    router.callback_query.register(on_stale_callback)
    router.message.register(on_unknown_message)
    return router
