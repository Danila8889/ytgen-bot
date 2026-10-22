"""Helpers shared by several flows."""
from __future__ import annotations

from aiogram import Bot

from .. import keyboards, schemas
from ..context import AppContext
from ..ui import fail, say


async def linked_me(
    ctx: AppContext, bot: Bot, uid: int, *, prompt: str, after_tariff: int = 0
) -> schemas.MeResponse | None:
    """The caller's /me when an account is linked; otherwise the link prompt is shown and None returned."""
    try:
        me = await ctx.wrapper.me(uid)
    except Exception as exc:
        await fail(ctx, bot, uid, exc, "me")
        return None
    if not me.linked or me.tenant is None:
        await say(ctx, bot, uid, prompt, keyboards.link_prompt(after_tariff))
        return None
    return me
