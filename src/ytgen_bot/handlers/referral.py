"""Referral screen and withdrawal request (wrapper `/referral`, `/referral/withdrawals`)."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, keyboards, schemas, texts
from ..context import AppContext
from ..ui import answer, fail, say
from .filters import IsAction
from .states import WithdrawStates

log = logging.getLogger(__name__)


_AMOUNT_RE = re.compile(r"^\d{1,9}$")
_LINK_KBS = {"link": keyboards.link_prompt()}


def _can_withdraw(info: schemas.ReferralInfo) -> bool:
    return info.open_withdrawal is None and info.balance.available_rub >= info.min_withdrawal_rub > 0


async def show_referral(ctx: AppContext, bot: Bot, uid: int) -> None:
    try:
        info = await ctx.wrapper.referral(uid)
    except Exception as exc:
        await fail(ctx, bot, uid, exc, "referral", kbs=_LINK_KBS)
        return
    if info.deep_link is None:
        # The wrapper has no BOT_DEEP_LINK_BASE: a deployment problem, loud, with no invented fallback link.
        log.error("referral_deep_link_missing", extra={"tg_user": uid})
    await say(
        ctx, bot, uid, texts.referral_screen(info, ctx.support), keyboards.referral_actions(_can_withdraw(info))
    )


async def cmd_referral(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_referral(ctx, bot, message.from_user.id)


async def on_withdraw_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    uid = callback.from_user.id
    await answer(callback)
    if action.args[0] == "cancel":
        await state.clear()
        await say(ctx, bot, uid, "Отменено.", keyboards.main_menu())
        return
    try:
        info = await ctx.wrapper.referral(uid)
    except Exception as exc:
        await fail(ctx, bot, uid, exc, "referral", kbs=_LINK_KBS)
        return
    if not _can_withdraw(info):
        await say(ctx, bot, uid, texts.withdraw_unavailable())
        return
    await state.set_state(WithdrawStates.amount)
    await say(ctx, bot, uid, texts.withdraw_ask(info))


async def on_amount(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    uid = message.from_user.id
    raw = re.sub(r"\s", "", message.text or "")
    if _AMOUNT_RE.fullmatch(raw) is None or int(raw) <= 0:
        await say(ctx, bot, uid, texts.withdraw_bad_amount())
        return
    amount = int(raw)
    await state.clear()
    await say(ctx, bot, uid, texts.withdraw_confirm(amount), keyboards.withdraw_confirm(amount))


async def on_withdraw_confirm(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    uid = callback.from_user.id
    amount = int(action.args[0] or 0)
    with ctx.guard.hold((uid, "withdraw")) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        try:
            result = await ctx.wrapper.request_withdrawal(uid, amount)
        except Exception as exc:
            view = await fail(ctx, bot, uid, exc, "withdrawal", kbs=_LINK_KBS)
            code = getattr(exc, "code", None)
            if code in ("withdrawal_below_minimum", "insufficient_balance"):
                await state.set_state(WithdrawStates.amount)  # the next message is a new amount
            else:
                await state.clear()
            log.info("withdrawal_refused", extra={"tg_user": uid, "code": code, "next": view.next})
            return
        await state.clear()
        log.info(
            "withdrawal_requested",
            extra={"tg_user": uid, "withdrawal_id": result.id, "amount_rub": result.amount_rub},
        )
        await say(ctx, bot, uid, texts.withdraw_done(result.amount_rub, result.id, ctx.support))


def build_router() -> Router:
    router = Router(name="referral")
    router.message.register(cmd_referral, Command("referral"))
    router.callback_query.register(on_withdraw_button, IsAction("withdraw"))
    router.message.register(on_amount, StateFilter(WithdrawStates.amount), F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_withdraw_confirm, IsAction("withdraw_ok"))
    return router
