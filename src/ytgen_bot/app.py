"""Wiring: dispatcher, startup checks, heartbeat, long polling."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from . import texts
from .ai.client import KieChatClient
from .ai.layer import AiLayer, Breaker
from .config import Settings
from .context import AppContext
from .db import migrate
from .db.engine import build_url, make_engine
from .db.store import PgStore, StoreError, utcnow
from .funnel.flow import Funnel
from .funnel.reminders import ReminderLoop
from .funnel.tariffs import (
    TariffContractError,
    TariffProvider,
    check_rows,
    parse_public_tariffs,
)
from .handlers import funnel, link, menu, payment, referral, start, tariffs
from .knowledge import load as load_knowledge
from .middlewares import ConsentMiddleware, GuardMiddleware
from .payments import PaymentWatchers
from .ui import say
from .wrapper import WRAPPER_FAILURES, WrapperClient, WrapperError, WrapperUnavailable

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
SELFCHECK_RETRY_DELAYS_S = (2.0, 5.0, 10.0, 20.0)  # runtime startup: the wrapper may be restarting
# The deploy `selfcheck` command only. The VPS -> api.telegram.org path loses packets intermittently (measured
# 2026-10-23: connects taking 1-6 s with SYN retransmits, ~1 in 6 not completing within 8 s), so a network-level
# error is retried on a SHORT per-attempt timeout (a fresh connection beats waiting on a stalled one): 6 attempts,
# worst case per call 6 x 10 s + 54 s of backoff = 114 s. Only network errors are retried; see `_retrying`.
SELFCHECK_NETWORK_RETRY_DELAYS_S = (3.0, 6.0, 10.0, 15.0, 20.0)
SELFCHECK_REQUEST_TIMEOUT_S = 10
SELFCHECK_DEADLINE_S = 300.0  # hard cap on the whole command (getMe + wrapper + DB + tariffs); fails loudly past it
PURGE_EVERY_S = 6 * 3600.0
AI_LOG_RETENTION_DAYS = 90


class SelfCheckError(RuntimeError):
    """A startup/deploy check failed. The message never contains a secret."""


def _error_label(exc: BaseException) -> str:
    """Error class names only (never the message, which may echo a URL): `TelegramNetworkError <- TimeoutError`."""
    label = type(exc).__name__
    if exc.__cause__ is not None:
        label += f" <- {type(exc.__cause__).__name__}"
    return label


async def _retrying[T](what: str, call: Callable[[], Awaitable[T]], delays: tuple[float, ...]) -> T:
    """Run `call`, retrying ONLY network-level failures (Telegram `TelegramNetworkError`, wrapper `WrapperUnavailable`:
    timeouts, connection errors) with the given backoff. Every retry is a WARNING with the attempt number and the error
    class; running out of attempts is a `SelfCheckError` that says how many were made. Anything else (a 401, a contract or
    database error, a non-network Telegram error) propagates untouched on the first attempt."""
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except (TelegramNetworkError, WrapperUnavailable) as exc:
            if attempt == attempts:
                noun = "attempt" if attempts == 1 else "attempts"
                raise SelfCheckError(
                    f"{what} unreachable after {attempts} {noun} (last error: {_error_label(exc)})"
                ) from None
            log.warning(
                "self_check_retry",
                extra={"target": what, "attempt": attempt, "attempts": attempts, "error": _error_label(exc),
                       "retry_in_s": delays[attempt - 1]},
            )
            await asyncio.sleep(delays[attempt - 1])
    raise AssertionError("unreachable")  # pragma: no cover


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
        funnel.build_router(),
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


async def self_check(
    bot: Bot,
    wrapper: WrapperClient,
    *,
    retry_delays: tuple[float, ...] = SELFCHECK_RETRY_DELAYS_S,
    telegram_retry_delays: tuple[float, ...] = (),
    request_timeout_s: int | None = None,
) -> str:
    """getMe, then one authenticated read-only call to the wrapper. Returns the bot's username.

    The wrapper call is `GET /v1/bot/me` for the bot's own id (never a real user; unlinked -> `linked:
    false`): 200 proves URL + key, 401 means the key differs from the wrapper's, 503 means the wrapper has the
    bot API switched off. Network failures are retried (the wrapper may be restarting; for the deploy command also
    Telegram, see `SELFCHECK_NETWORK_RETRY_DELAYS_S`); the rest is final on the first attempt. The runtime startup
    passes neither `telegram_retry_delays` nor `request_timeout_s`, so its behaviour is unchanged.
    """
    try:
        me = await _retrying(
            "Telegram getMe", lambda: bot.get_me(request_timeout=request_timeout_s), telegram_retry_delays
        )
    except TelegramUnauthorizedError:
        raise SelfCheckError("Telegram rejected BOT_TOKEN (401 Unauthorized)") from None
    if not me.username:
        raise SelfCheckError("getMe returned no bot username (the return URL needs it)")
    try:
        await _retrying("wrapper (WRAPPER_BASE_URL)", lambda: wrapper.me(me.id), retry_delays)
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


async def funnel_self_check(
    settings: Settings, wrapper: WrapperClient, *, retry_delays: tuple[float, ...] = ()
) -> None:
    """The funnel's deploy checks: the database is reachable AND migrated to head, and the wrapper's public tariff list has
    the contract the funnel needs (`is_monthly` + a complete `capacity` on every row). The AI provider is NOT called: only
    its configuration (already validated at startup) is checked. A network-level failure reading the wrapper's tariffs
    is retried with `retry_delays`; the database and the contract checks are never retried."""
    engine = make_engine(build_url(settings))
    try:
        store = PgStore(engine)
        try:
            await store.ping()
            if not await migrate.at_head(engine):
                raise SelfCheckError(
                    f"database migrations are not at head (current {await migrate.current_revision(engine)}, "
                    f"head {migrate.head_revision()})"
                )
        except StoreError:
            raise SelfCheckError("the funnel database is not reachable") from None
        except OSError:
            raise SelfCheckError("the funnel database is not reachable") from None
    finally:
        await engine.dispose()
    try:
        raw = await _retrying("wrapper public tariffs", wrapper.public_tariffs, retry_delays)
    except (WrapperUnavailable, WrapperError) as exc:
        raise SelfCheckError(f"could not read the wrapper's public tariffs ({type(exc).__name__})") from None
    except WRAPPER_FAILURES:
        raise SelfCheckError("the wrapper's public tariff list is not valid JSON") from None
    try:
        check_rows(raw)
    except TariffContractError as exc:
        raise SelfCheckError(f"wrapper tariff contract: {exc}") from None
    try:
        parse_public_tariffs(raw)
    except TariffContractError as exc:
        # Not a deploy blocker (the funnel then falls back to the manager) but never silent.
        log.error("funnel_tariff_roles_invalid", extra={"problem": str(exc)})


async def run_selfcheck(settings: Settings) -> None:
    bot = new_bot(settings)
    wrapper = WrapperClient(settings.wrapper_base_url, settings.service_key, timeout_s=SELFCHECK_REQUEST_TIMEOUT_S)
    try:
        deadline = asyncio.timeout(SELFCHECK_DEADLINE_S)
        try:
            async with deadline:
                await self_check(
                    bot, wrapper, retry_delays=SELFCHECK_NETWORK_RETRY_DELAYS_S,
                    telegram_retry_delays=SELFCHECK_NETWORK_RETRY_DELAYS_S, request_timeout_s=SELFCHECK_REQUEST_TIMEOUT_S,
                )
                await funnel_self_check(settings, wrapper, retry_delays=SELFCHECK_NETWORK_RETRY_DELAYS_S)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise SelfCheckError(f"the self-check did not finish within {SELFCHECK_DEADLINE_S:.0f} s overall") from None
        log.info("funnel_self_check_ok")
    finally:
        await wrapper.aclose()
        await bot.session.close()


def _spawn(coro: Awaitable[None], name: str) -> asyncio.Task[None]:
    """A background task whose crash is an ERROR in the log (never a silently dead loop)."""

    def done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("background_task_crashed", exc_info=task.exception(), extra={"task": name})

    task = asyncio.ensure_future(coro)
    task.set_name(name)
    task.add_done_callback(done)
    return task


async def purge_loop(store: PgStore, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Retention: delete AI-log rows older than 90 days. In-process, idempotent, an ERROR (from the store) on failure."""
    while True:
        try:
            removed = await store.purge_ai_log(utcnow() - dt.timedelta(days=AI_LOG_RETENTION_DAYS))
            log.info("ai_log_purged", extra={"removed": removed})
        except StoreError:
            pass  # already logged at ERROR by the store; retried on the next round
        await sleep(PURGE_EVERY_S)


def build_funnel(settings: Settings, wrapper: WrapperClient, store: PgStore, notify: Callable[[str], Awaitable[None]]):
    tariffs = TariffProvider(wrapper.public_tariffs, settings.tariff_cache_ttl_s)
    kb = load_knowledge(settings.support_contact)
    client = KieChatClient(
        api_key=settings.kie_api_key, base_url=settings.kie_api_url, model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
    )
    ai = AiLayer(
        store=store, client=client, tariffs=tariffs, kb=kb, user_hourly_limit=settings.ai_user_hourly_limit,
        daily_ceiling=settings.ai_daily_ceiling, max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        breaker=Breaker(settings.ai_breaker_failures, settings.ai_breaker_cooldown_min * 60.0),
        notify_manager=notify, clock=utcnow,
    )
    funnel = Funnel(
        store=store, wrapper=wrapper, tariffs=tariffs, kb=kb, ai=ai, support=settings.support_contact,
        manager_chat_id=settings.manager_chat_id, clock=utcnow,
    )
    return funnel, client


async def run_bot(settings: Settings) -> None:
    bot = new_bot(settings)
    wrapper = WrapperClient(settings.wrapper_base_url, settings.service_key)
    engine = make_engine(build_url(settings))
    store = PgStore(engine)
    tasks: list[asyncio.Task[None]] = []
    ai_client: KieChatClient | None = None
    ctx: AppContext | None = None
    try:
        username = await self_check(bot, wrapper)

        # ISOLATION: the funnel database being down or behind must not stop checkout / payment status / link / referral /
        # withdrawal from working, so a failed check here is an ERROR log, not a refusal to start (the deploy self-check
        # is what blocks a bad release). Funnel flows then show "something went wrong, write to the manager".
        try:
            await store.ping()
            if not await migrate.at_head(engine):
                log.error("funnel_db_not_at_head", extra={"head": migrate.head_revision()})
        except (StoreError, OSError):
            log.error("funnel_db_unavailable_at_startup")

        async def notify(text: str) -> None:
            await bot.send_message(settings.manager_chat_id, text)

        funnel, ai_client = build_funnel(settings, wrapper, store, notify)
        ctx = AppContext(settings=settings, wrapper=wrapper, bot_username=username, funnel=funnel)
        ctx.watchers = PaymentWatchers(ctx, bot)
        dp = build_dispatcher(ctx)
        await bot.set_my_commands(COMMANDS)
        await bot.delete_webhook(drop_pending_updates=False)  # polling and a webhook exclude each other
        tasks.append(_spawn(_heartbeat(bot, settings.heartbeat_file), "heartbeat"))
        tasks.append(_spawn(ReminderLoop(funnel, ctx, bot).run(), "reminders"))
        tasks.append(_spawn(purge_loop(store), "ai_log_purge"))
        log.info("polling_started", extra={"bot_username": username})
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if ctx is not None and ctx.watchers is not None:
            await ctx.watchers.shutdown()
        if ai_client is not None:
            await ai_client.aclose()
        await engine.dispose()
        await wrapper.aclose()
        await bot.session.close()
