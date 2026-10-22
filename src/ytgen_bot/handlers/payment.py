"""The payment-status screen: return from the payment page (`/start paid`) and the "check payment" button."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

from .. import callbacks, keyboards, texts
from ..context import AppContext
from ..payments import announce
from ..ui import answer, fail, say
from .filters import IsAction

log = logging.getLogger(__name__)



async def show_paid_return(ctx: AppContext, bot: Bot, uid: int) -> None:
    """`/start paid`: the redirect carries no payment id, so show the account's current state."""
    try:
        me = await ctx.wrapper.me(uid)
    except Exception as exc:
        await fail(ctx, bot, uid, exc, "me")
        return
    await say(ctx, bot, uid, texts.back_from_payment(me), keyboards.status_actions(me.linked))


async def on_check(callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext) -> None:
    uid = callback.from_user.id
    payment_id = action.args[0]
    assert payment_id is not None
    with ctx.guard.hold((uid, "check", payment_id)) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        try:
            status = await ctx.wrapper.payment_status(uid, payment_id)
        except Exception as exc:
            await answer(callback)
            await fail(ctx, bot, uid, exc, "payment")
            return
        log.info(
            "payment_checked",
            extra={"tg_user": uid, "payment_id": payment_id, "status": status.status, "credited": status.credited},
        )
        if await announce(ctx, bot, uid, status):
            if ctx.watchers is not None:
                ctx.watchers.stop(payment_id)
            await answer(callback)
            return
        if status.status == "succeeded":
            await answer(callback)
            await say(ctx, bot, uid, texts.payment_credit_delayed(), keyboards.check_payment(payment_id))
        else:
            await answer(callback, texts.payment_still_pending())
        if ctx.watchers is not None and not ctx.watchers.active(payment_id):
            ctx.watchers.watch(uid, payment_id)


def build_router() -> Router:
    router = Router(name="payment")
    router.callback_query.register(on_check, IsAction("check"))
    return router
