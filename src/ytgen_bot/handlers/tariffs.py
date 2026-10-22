"""Tariffs -> optional promo code -> quote -> checkout -> payment link, and the payment follow-up."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, keyboards, texts
from ..context import AppContext
from ..ui import answer, fail, say
from ..wrapper import WRAPPER_FAILURES
from .common import linked_me
from .filters import IsAction
from .states import PromoStates

log = logging.getLogger(__name__)


_PROMO_INPUT_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")


async def show_tariffs(ctx: AppContext, bot: Bot, uid: int) -> None:
    try:
        items = await ctx.wrapper.tariffs(uid)
    except WRAPPER_FAILURES as exc:
        await fail(ctx, bot, uid, exc, "generic")
        return
    await say(ctx, bot, uid, texts.tariffs_list(items), keyboards.tariffs(items) if items else None)


async def choose_tariff(ctx: AppContext, bot: Bot, uid: int, tariff_id: int) -> None:
    """A tariff was picked: require a linked account, then offer the promo-code step."""
    me = await linked_me(ctx, bot, uid, prompt=texts.link_need_account(), after_tariff=tariff_id)
    if me is None:
        return
    try:
        items = await ctx.wrapper.tariffs(uid)
    except WRAPPER_FAILURES as exc:
        await fail(ctx, bot, uid, exc, "generic")
        return
    tariff = next((t for t in items if t.id == tariff_id), None)
    if tariff is None:
        await say(ctx, bot, uid, "Этот тариф сейчас недоступен. Откройте список заново: /tariffs")
        return
    await say(ctx, bot, uid, texts.tariff_chosen(tariff), keyboards.tariff_actions(tariff.id))


async def do_quote(
    ctx: AppContext, bot: Bot, uid: int, tariff_id: int, promo_code: str | None
) -> None:
    with ctx.guard.hold((uid, "quote", tariff_id, promo_code), cooldown=False) as acquired:
        if not acquired:
            await say(ctx, bot, uid, texts.ALREADY_PROCESSING)
            return
        try:
            quote = await ctx.wrapper.quote(uid, tariff_id, promo_code)
        except WRAPPER_FAILURES as exc:
            await fail(
                ctx, bot, uid, exc, "quote",
                kbs={"retry": keyboards.tariff_actions(tariff_id), "link": keyboards.link_prompt(tariff_id)},
            )
            return
    if quote.active_tariff_mismatch:
        # The wrapper says checkout of this tariff would be refused (an active different tariff): no pay button.
        log.info("active_other_tariff", extra={"tg_user": uid, "tariff_id": tariff_id})
        await say(ctx, bot, uid, texts.active_other_tariff(quote.tariff_expires_at))
        return
    promo = promo_code if quote.promo is not None else None
    await say(
        ctx, bot, uid, texts.quote_text(quote),
        keyboards.pay_confirm(quote.tariff_id, quote.payable_rub, promo),
    )


async def cmd_tariffs(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_tariffs(ctx, bot, message.from_user.id)


async def on_tariff(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    await answer(callback)
    await state.clear()
    await choose_tariff(ctx, bot, callback.from_user.id, int(action.args[0] or 0))


async def on_promo_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    await answer(callback)
    await state.set_state(PromoStates.code)
    await state.update_data(tariff_id=int(action.args[0] or 0))
    await say(ctx, bot, callback.from_user.id, texts.promo_ask())


async def on_promo_text(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    uid = message.from_user.id
    promo = (message.text or "").strip().upper()
    if _PROMO_INPUT_RE.fullmatch(promo) is None:
        await say(ctx, bot, uid, texts.promo_bad_format())
        return
    tariff_id = int((await state.get_data()).get("tariff_id", 0))
    await state.clear()
    if not tariff_id:
        await say(ctx, bot, uid, texts.STALE_BUTTON)
        return
    await do_quote(ctx, bot, uid, tariff_id, promo)


async def on_quote_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    await answer(callback)
    await state.clear()
    await do_quote(ctx, bot, callback.from_user.id, int(action.args[0] or 0), None)


async def on_pay(callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext) -> None:
    uid = callback.from_user.id
    tariff_id, expected_rub, promo = int(action.args[0] or 0), int(action.args[1] or 0), action.args[2]
    # Double-tap debounce: one checkout per (user, tariff, promo) in flight, and a short cooldown after it.
    with ctx.guard.hold((uid, "pay", tariff_id, promo)) as acquired:
        if not acquired:
            await answer(callback, texts.ALREADY_PROCESSING)
            return
        await answer(callback)
        try:
            checkout = await ctx.wrapper.checkout(uid, tariff_id, ctx.return_url, promo)
        except WRAPPER_FAILURES as exc:
            await fail(
                ctx, bot, uid, exc, "checkout",
                kbs={"retry": keyboards.tariff_actions(tariff_id), "link": keyboards.link_prompt(tariff_id)},
            )
            return
        if not checkout.payment_url.startswith("https://"):
            log.error("checkout_payment_url_not_https", extra={"tg_user": uid, "payment_id": checkout.payment_id})
            await say(ctx, bot, uid, texts.GENERIC_ERROR)
            return
        log.info(
            "checkout_created",
            extra={
                "tg_user": uid,
                "payment_id": checkout.payment_id,
                "tariff_id": checkout.tariff_id,
                "amount_rub": checkout.amount_rub,
                "reused": checkout.reused,
                "promo": promo is not None,
            },
        )
        head = (
            texts.checkout_reused(checkout.amount_rub)
            if checkout.reused
            else texts.checkout_created(checkout.amount_rub, expected_rub)
        )
        await say(
            ctx, bot, uid, f"{head}\n\n{texts.processing()}",
            keyboards.payment(checkout.payment_url, checkout.payment_id, checkout.amount_rub),
        )
        if ctx.watchers is not None:
            ctx.watchers.watch(uid, checkout.payment_id)


def build_router() -> Router:
    router = Router(name="tariffs")
    router.message.register(cmd_tariffs, Command("tariffs"))
    router.callback_query.register(on_tariff, IsAction("tariff"))
    router.callback_query.register(on_promo_button, IsAction("promo"))
    router.message.register(on_promo_text, StateFilter(PromoStates.code), F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_quote_button, IsAction("quote"))
    router.callback_query.register(on_pay, IsAction("pay"))
    return router
