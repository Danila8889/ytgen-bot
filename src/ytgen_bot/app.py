"""Wiring: dispatcher, startup checks, heartbeat, long polling."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from . import texts
from .config import Settings
from .context import AppContext
from .handlers import link, menu, payment, referral, start, tariffs
from .middlewares import ConsentMiddleware, GuardMiddleware
from .payments import PaymentWatchers
from .ui import say
from .wrapper import WrapperClient, WrapperError, WrapperUnavailable

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="tariffs", description="Тарифы и оплата"),
    BotCommand(command="referral", description="Реферальная программа"),
    BotCommand(command="status", description="Аккаунт и тариф"),
    BotCommand(command="stop", description="Отказаться от сообщений бота"),
    BotCommand(command="help", description="Помощь"),
]
HEARTBEAT_EVERY_S = 60.0
SELFCHECK_RETRY_DELAYS_S = (2.0, 5.0, 10.0, 20.0)


class SelfCheckError(RuntimeError):
    """A startup/deploy check failed. The message never contains a secret."""


def new_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), ctx=ctx)
    dp.message.outer_middleware(GuardMiddleware(ctx))
    dp.callback_query.outer_middleware(GuardMiddleware(ctx))

    gated = Router(name="gated")
    gated.message.middleware(ConsentMiddleware(ctx))
    gated.callback_query.middleware(ConsentMiddleware(ctx))
    # Order matters: commands and buttons first, FSM text inputs next, the catch-alls (menu) last.
    gated.include_routers(
        payment.build_router(),
        tariffs.build_router(),
        referral.build_router(),
        link.build_router(),
        menu.build_router(),
    )

    dp.include_router(start.build_router())  # /start, consent, /stop: not behind the consent gate
    dp.include_router(gated)
    dp.errors.register(on_error)
    return dp


async def on_error(event: ErrorEvent, ctx: AppContext, bot: Bot) -> bool:
    update = event.update
    user = None
    if update.message is not None:
        user = update.message.from_user
    elif update.callback_query is not None:
        user = update.callback_query.from_user
    uid = user.id if user is not None else None
    log.error(
        "unhandled_error",
        exc_info=event.exception,
        extra={"update_id": update.update_id, "tg_user": uid},
    )
    if uid is not None:
        await say(ctx, bot, uid, texts.GENERIC_ERROR)
    return True


async def self_check(bot: Bot, wrapper: WrapperClient, *, retry_delays: tuple[float, ...] = SELFCHECK_RETRY_DELAYS_S) -> str:
    """getMe, then one authenticated read-only call to the wrapper. Returns the bot's username.

    The wrapper call is `GET /v1/bot/me` for the bot's own id (never a real user; unlinked -> `linked:
    false`): 200 proves URL + key, 401 means the key differs from the wrapper's, 503 means the wrapper has the
    bot API switched off. Network failures are retried (the wrapper may be restarting); the rest is final.
    """
    me = await bot.get_me()
    if not me.username:
        raise SelfCheckError("getMe returned no bot username (the return URL needs it)")
    delays = list(retry_delays)
    while True:
        try:
            await wrapper.me(me.id)
            break
        except WrapperUnavailable:
            if not delays:
                raise SelfCheckError("wrapper unreachable at WRAPPER_BASE_URL") from None
            await asyncio.sleep(delays.pop(0))
        except WrapperError as exc:
            if exc.code == "invalid_bot_service_key":
                raise SelfCheckError(
                    "wrapper rejected BOT_SERVICE_API_KEY (it must equal the wrapper's BOT_SERVICE_API_KEY)"
                ) from None
            if exc.code == "bot_api_disabled":
                raise SelfCheckError(
                    "wrapper bot API is disabled (BOT_SERVICE_API_KEY unset or too short on the wrapper)"
                ) from None
            raise SelfCheckError(f"wrapper answered {exc.status} {exc.code} to the self-check") from None
    log.info("self_check_ok", extra={"bot_username": me.username})
    return me.username


async def _heartbeat(bot: Bot, path: str) -> None:
    """Touch the heartbeat file only while Telegram answers getMe; the container healthcheck reads its age."""
    file = Path(path)
    while True:
        try:
            await bot.get_me()
            file.write_text(str(int(time.time())))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat_failed")
        await asyncio.sleep(HEARTBEAT_EVERY_S)


async def run_selfcheck(settings: Settings) -> None:
    bot = new_bot(settings)
    wrapper = WrapperClient(settings.wrapper_base_url, settings.service_key)
    try:
        await self_check(bot, wrapper, retry_delays=SELFCHECK_RETRY_DELAYS_S[:2])
    finally:
        await wrapper.aclose()
        await bot.session.close()


async def run_bot(settings: Settings) -> None:
    bot = new_bot(settings)
    wrapper = WrapperClient(settings.wrapper_base_url, settings.service_key)
    heartbeat: asyncio.Task[None] | None = None
    ctx: AppContext | None = None
    try:
        username = await self_check(bot, wrapper)
        ctx = AppContext(settings=settings, wrapper=wrapper, bot_username=username)
        ctx.watchers = PaymentWatchers(ctx, bot)
        dp = build_dispatcher(ctx)
        await bot.set_my_commands(COMMANDS)
        await bot.delete_webhook(drop_pending_updates=False)  # polling and a webhook exclude each other
        heartbeat = asyncio.create_task(_heartbeat(bot, settings.heartbeat_file), name="heartbeat")
        log.info("polling_started", extra={"bot_username": username})
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if ctx is not None and ctx.watchers is not None:
            await ctx.watchers.shutdown()
        await wrapper.aclose()
        await bot.session.close()
