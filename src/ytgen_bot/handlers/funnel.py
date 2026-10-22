"""Glue between aiogram and the funnel: its buttons (`f:<code>`, `rm:yes|no`) and the free text of funnel participants.

Behind the consent gate like every other handler except /start, the consent button and /stop. Registered AFTER the
FSM-driven routers (link email/code, promo, withdrawal) so their text inputs win, and BEFORE the menu fallbacks.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, texts
from ..context import AppContext
from ..ui import answer, say
from .filters import IsAction
from .menu import on_unknown_message


async def on_funnel_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    uid = callback.from_user.id
    await state.clear()  # a funnel button leaves whatever text prompt (promo, email, ...) was open
    code = action.args[0]
    assert code is not None
    # One funnel action in flight per person (concurrent updates must not interleave on the same lead).
    with ctx.guard.hold((uid, "funnel"), cooldown=False) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        await ctx.funnel.on_button(ctx, bot, uid, code, callback.from_user)


async def on_reminders_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    uid = callback.from_user.id
    await state.clear()
    choice = action.args[0]
    assert choice is not None
    with ctx.guard.hold((uid, "funnel"), cooldown=False) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        await ctx.funnel.on_reminders_answer(ctx, bot, uid, choice)


async def on_free_text(message: Message, bot: Bot, ctx: AppContext) -> None:
    uid = message.from_user.id
    assert message.text is not None
    with ctx.guard.hold((uid, "funnel"), cooldown=False) as acquired:
        if not acquired:
            await say(ctx, bot, uid, texts.ALREADY_PROCESSING)
            return
        handled = await ctx.funnel.on_text(ctx, bot, uid, message.text, message.from_user)
    if not handled:  # not a funnel participant (e.g. already a customer): the generic reply, as before
        await on_unknown_message(message, bot, ctx)


def build_router() -> Router:
    router = Router(name="funnel")
    router.callback_query.register(on_funnel_button, IsAction("funnel"))
    router.callback_query.register(on_reminders_button, IsAction("reminders"))
    router.message.register(on_free_text, StateFilter(None), F.text, ~F.text.startswith("/"))
    return router
