"""/start (consent step, referral code, return from payment), the consent button and /stop.

These handlers are NOT behind the consent gate: they are how consent is given and withdrawn.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from .. import callbacks, keyboards, schemas, texts
from ..context import AppContext
from ..db.store import StoreError
from ..funnel import texts as funnel_texts
from ..funnel.model import MAX_PAID_NOTE
from ..startparam import StartParam, parse_start_param
from ..ui import answer, fail, say
from ..wrapper import WRAPPER_FAILURES
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


async def _funnel_or_menu(
    ctx: AppContext, bot: Bot, user: User, me: schemas.MeResponse, param: StartParam,
    note: str | None, *, given: bool,
) -> None:
    """After consent: the sales funnel for people without paid access, the existing menu for everyone else.

    ISOLATION: the funnel database or logic failing must never take the rest of the bot with it: the person is told so
    and gets the menu (tariffs / payment / referral still work).
    """
    uid = user.id
    paid = me.linked and me.tenant is not None and me.tenant.paid_access_active
    if paid:
        # Skips the sales funnel (isolated hook: marks an existing lead paid, M5.6 once); then the existing menu.
        await ctx.funnel.on_paid_start(ctx, bot, uid, me)
        await _welcome(ctx, bot, uid, note, given=given)
        return
    try:
        if not given and not await ctx.funnel.accepted(uid):
            # Consented to the wrapper before the funnel existed: the funnel's data processing needs its own acceptance.
            text = texts.consent_request()
            await say(
                ctx, bot, uid, f"{note}\n\n{text}" if note else text,
                keyboards.consent(param.ref_code, param.source),
            )
            return
        if given:
            await say(ctx, bot, uid, "\n\n".join([texts.consent_given(), *([note] if note else [])]))
        if await ctx.funnel.entry(ctx, bot, uid, user, source=param.source, me=me):
            return
    except StoreError:
        await say(ctx, bot, uid, f"{funnel_texts.funnel_error(ctx.support)}\n\n{texts.menu_prompt()}", keyboards.main_menu())
        return
    await _welcome(ctx, bot, uid, note, given=given)


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
    except WRAPPER_FAILURES as exc:
        await fail(ctx, bot, uid, exc, "generic")
        return

    log.info(
        "start",
        extra={"tg_user": uid, "param": param.kind, "consented": me.consented, "linked": me.linked},
    )
    if result is None:
        await say(
            ctx, bot, uid, texts.consent_request(),
            keyboards.consent(param.ref_code if param.kind == "ref" else None, param.source),
        )
        return
    if param.ref_code and result.referral is None:
        log.warning("referral_outcome_missing", extra={"tg_user": uid})
    if param.kind == "paid":
        await show_paid_return(ctx, bot, uid)
        return
    await _funnel_or_menu(ctx, bot, message.from_user, me, param, _referral_note(result), given=False)


async def on_consent(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext
) -> None:
    uid = callback.from_user.id
    ref_code, source = action.args[0], action.args[1]
    with ctx.guard.hold((uid, "consent")) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        try:
            result = await ctx.wrapper.start(
                uid, language=callback.from_user.language_code, consent=True, referral_code=ref_code
            )
        except WRAPPER_FAILURES as exc:
            await fail(ctx, bot, uid, exc, "generic")
            return
        if not result.consented:
            log.error("consent_not_recorded", extra={"tg_user": uid})
            await say(ctx, bot, uid, texts.GENERIC_ERROR)
            return
        log.info("consent_given", extra={"tg_user": uid, "has_ref": ref_code is not None})
        try:
            me = await ctx.wrapper.me(uid)
        except WRAPPER_FAILURES as exc:
            await fail(ctx, bot, uid, exc, "generic")
            return
        param = StartParam("ref", ref_code=ref_code) if ref_code else (
            StartParam("source", source=source) if source else StartParam("none")
        )
        await _funnel_or_menu(ctx, bot, callback.from_user, me, param, _referral_note(result), given=True)


async def cmd_stop(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    wrapper_failure: Exception | None = None
    try:
        await ctx.wrapper.opt_out(uid)
    except WRAPPER_FAILURES as exc:
        wrapper_failure = exc
    # Deleting the funnel data does not depend on the wrapper: it is done either way (idempotent), and a failure is
    # shown, never swallowed.
    purge_failed = False
    try:
        await ctx.funnel.on_stop(uid)
    except StoreError:
        purge_failed = True
    if wrapper_failure is not None:
        # No fake success: the opt-out is not recorded until the wrapper says so.
        await fail(ctx, bot, uid, wrapper_failure, "generic")
        return
    log.info("opted_out", extra={"tg_user": uid, "funnel_purge_failed": purge_failed})
    text = texts.stopped()
    if purge_failed:
        text += "\n\n" + texts.stop_purge_failed(ctx.support)
    await say(ctx, bot, uid, text)


async def cmd_paid(message: Message, command: CommandObject, bot: Bot, ctx: AppContext) -> None:
    """`/paid <tg_id> [note]`: the manager marks a lead paid for a payment made outside the bot. Authorised strictly by
    the CHAT id (MANAGER_CHAT_ID); anyone else is ignored without a reply (fail closed), with an INFO log."""
    if message.chat.id != ctx.settings.manager_chat_id:
        log.info("paid_command_denied", extra={"tg_user": message.from_user.id if message.from_user else None})
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await say(ctx, bot, message.chat.id, "Формат: /paid <tg_id> [заметка]")
        return
    note = parts[1].strip()[:MAX_PAID_NOTE] if len(parts) > 1 else None
    reply = await ctx.funnel.manager_paid(ctx, bot, int(parts[0]), note)
    await say(ctx, bot, message.chat.id, reply)


def build_router() -> Router:
    router = Router(name="public")
    router.message.register(cmd_start, CommandStart())
    router.callback_query.register(on_consent, IsAction("consent"))
    router.message.register(cmd_stop, Command("stop"))
    router.message.register(cmd_paid, Command("paid"))
    return router
