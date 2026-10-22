"""Account link: email -> emailed code -> verified link (wrapper `/link/request-code`, `/link/verify`)."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import callbacks, keyboards, texts
from ..context import AppContext
from ..masking import mask_code, mask_email
from ..ui import answer, fail, say
from ..wrapper import WRAPPER_FAILURES
from .filters import IsAction
from .states import LinkStates
from .tariffs import choose_tariff

log = logging.getLogger(__name__)


# Mirrors the wrapper's own shape check (src/bot/linking.py): the wrapper stays the authority, this only
# saves a pointless round trip (and a rate-limit slot) on an obvious typo.
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MAX_EMAIL_LENGTH = 254
_CODE_RE = re.compile(r"^\d{6}$")


async def begin_link(ctx: AppContext, bot: Bot, uid: int, state: FSMContext, after_tariff: int = 0) -> None:
    await state.clear()
    await state.set_state(LinkStates.email)
    await state.update_data(after_tariff=after_tariff)
    await say(ctx, bot, uid, texts.link_ask_email())


async def on_link_button(
    callback: CallbackQuery, action: callbacks.Action, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    await answer(callback)
    await begin_link(ctx, bot, callback.from_user.id, state, int(action.args[0] or 0))


async def on_email(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    uid = message.from_user.id
    email = (message.text or "").strip()
    if len(email) > MAX_EMAIL_LENGTH or _EMAIL_RE.fullmatch(email) is None:
        await say(ctx, bot, uid, texts.link_bad_email())
        return
    after_tariff = int((await state.get_data()).get("after_tariff", 0))
    with ctx.guard.hold((uid, "link_request"), cooldown=False) as acquired:
        if not acquired:
            await say(ctx, bot, uid, texts.ALREADY_PROCESSING)
            return
        try:
            result = await ctx.wrapper.request_code(uid, email)
        except WRAPPER_FAILURES as exc:
            view = await fail(ctx, bot, uid, exc, "link_request")
            if view.next == "none":
                await state.clear()
            return
    log.info("link_code_requested", extra={"tg_user": uid, "email": mask_email(email)})
    await state.set_state(LinkStates.code)
    await state.update_data(after_tariff=after_tariff)
    await say(
        ctx, bot, uid, texts.link_code_sent(mask_email(email), result.expires_in_s),
        keyboards.link_retry(after_tariff),
    )


async def on_code(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    uid = message.from_user.id
    raw = message.text or ""
    code = re.sub(r"[\s-]", "", raw)
    if _CODE_RE.fullmatch(code) is None:
        await say(ctx, bot, uid, texts.link_bad_code_format())
        return
    after_tariff = int((await state.get_data()).get("after_tariff", 0))
    with ctx.guard.hold((uid, "link_verify"), cooldown=False) as acquired:
        if not acquired:
            await say(ctx, bot, uid, texts.ALREADY_PROCESSING)
            return
        try:
            result = await ctx.wrapper.verify_code(uid, code)
        except WRAPPER_FAILURES as exc:
            log.info("link_code_rejected", extra={"tg_user": uid, "code": mask_code(code)})
            view = await fail(
                ctx, bot, uid, exc, "link_verify", kbs={"restart": keyboards.link_retry(after_tariff)}
            )
            if view.next in ("restart", "none"):
                await state.clear()
            return
    await state.clear()
    log.info(
        "link_verified",
        extra={"tg_user": uid, "already_linked": result.already_linked, "tenant_created": result.tenant_created},
    )
    parts = [texts.link_done(result)]
    if result.referral is not None:
        note = texts.referral_outcome(result.referral.outcome)
        if note:
            parts.append(note)
    await say(ctx, bot, uid, "\n\n".join(parts))
    if after_tariff:
        await choose_tariff(ctx, bot, uid, after_tariff)
    else:
        await say(ctx, bot, uid, texts.menu_prompt(), keyboards.main_menu())


def build_router() -> Router:
    router = Router(name="link")
    router.callback_query.register(on_link_button, IsAction("link"))
    router.message.register(on_email, StateFilter(LinkStates.email), F.text, ~F.text.startswith("/"))
    router.message.register(on_code, StateFilter(LinkStates.code), F.text, ~F.text.startswith("/"))
    return router
