"""/start (consent step, referral code, return from payment), the consent button and /stop.

These handlers are NOT behind the consent gate: they are how consent is given and withdrawn.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, keyboards, schemas, texts
from ..context import AppContext
from ..startparam import parse_start_param
from ..ui import answer, fail, say
from .filters import IsAction
from .payment import show_paid_return

log = logging.getLogger(__name__)



def _referral_note(result: schemas.StartResponse) -> str | None:
    if result.referral is None:
        return None
    return texts.referral_outcome(result.referral.outcome)


async def _welcome(ctx: AppContext, bot: Bot, uid: int, note: str | None, *, given: bool) -> None:
    parts = [texts.consent_given() if given else texts.menu_prompt()]
    if note:
        parts.append(note)
    if given:
        parts.append(texts.menu_prompt())
    await say(ctx, bot, uid, "\n\n".join(parts), keyboards.main_menu())


async def cmd_start(
    message: Message, command: CommandObject, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    await state.clear()
    uid = message.from_user.id
    language = message.from_user.language_code
    param = parse_start_param(command.args)
    if param.kind == "invalid":
        log.warning("start_param_invalid", extra={"tg_user": uid})

    try:
        # Consent is read with GET /me (read-only: no row, no `start` event). Nothing is written to the
        # wrapper for a user who has not consented yet, and the referral code is only ever sent after consent.
        me = await ctx.wrapper.me(uid)
        result: schemas.StartResponse | None = None
        if me.consented:
            result = await ctx.wrapper.start(uid, language=language, referral_code=param.ref_code)
    except Exception as exc:
        await fail(ctx, bot, uid, exc, "generic")
        return

    log.info(
        "start",
        extra={"tg_user": uid, "param": param.kind, "consented": me.consented, "linked": me.linked},
    )
    if result is None:
        await say(
            ctx, bot, uid, texts.consent_request(),
            keyboards.consent(param.ref_code if param.kind == "ref" else None),
        )
        return
    if param.ref_code and result.referral is None:
        log.warning("referral_outcome_missing", extra={"tg_user": uid})
    if param.kind == "paid":
        await show_paid_return(ctx, bot, uid)
        return
    await _welcome(ctx, bot, uid, _referral_note(result), given=False)


async def on_consent(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext
) -> None:
    uid = callback.from_user.id
    ref_code = action.args[0]
    with ctx.guard.hold((uid, "consent")) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        try:
            result = await ctx.wrapper.start(
                uid, language=callback.from_user.language_code, consent=True, referral_code=ref_code
            )
        except Exception as exc:
            await fail(ctx, bot, uid, exc, "generic")
            return
        if not result.consented:
            log.error("consent_not_recorded", extra={"tg_user": uid})
            await say(ctx, bot, uid, texts.GENERIC_ERROR)
            return
        log.info("consent_given", extra={"tg_user": uid, "has_ref": ref_code is not None})
        await _welcome(ctx, bot, uid, _referral_note(result), given=True)


async def cmd_stop(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    try:
        await ctx.wrapper.opt_out(uid)
    except Exception as exc:
        # No fake success: the opt-out is not recorded until the wrapper says so.
        await fail(ctx, bot, uid, exc, "generic")
        return
    log.info("opted_out", extra={"tg_user": uid})
    await say(ctx, bot, uid, texts.stopped())


def build_router() -> Router:
    router = Router(name="public")
    router.message.register(cmd_start, CommandStart())
    router.callback_query.register(on_consent, IsAction("consent"))
    router.message.register(cmd_stop, Command("stop"))
    return router
