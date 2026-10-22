"""Payment result handling: the bounded background poller and the shared terminal-state messages.

The poller lives in this process only. After a restart nothing is lost: every payment message carries a
"check payment" button whose callback data holds the payment id, and pressing it starts a fresh watch.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator

from aiogram import Bot

from . import keyboards, schemas, texts
from .context import AppContext
from .ui import fail, say
from .wrapper import WrapperContractError, WrapperError, WrapperUnavailable

log = logging.getLogger(__name__)

WATCH_TOTAL_S = 600.0
MAX_CONSECUTIVE_FAILURES = 5
MAX_WATCHERS = 200


def backoff_delays(total_s: float = WATCH_TOTAL_S, first: float = 3.0, factor: float = 1.5, cap: float = 30.0) -> Iterator[float]:
    """Delays between polls: 3, 4.5, 6.75 ... capped at 30 s, finite (the sum reaches `total_s`)."""
    elapsed, delay = 0.0, first
    while elapsed < total_s:
        step = min(delay, cap, total_s - elapsed)
        yield step
        elapsed += step
        delay *= factor


async def announce(ctx: AppContext, bot: Bot, uid: int, status: schemas.PaymentStatus) -> bool:
    """Send the final message for a terminal payment; False when it is not terminal yet.

    A user who opted out (/stop) is not messaged: the result is only logged. If their state cannot be read the
    message is sent anyway (it answers a payment they started).
    """
    succeeded = status.status == "succeeded" and status.credited
    if status.status != "canceled" and not succeeded:
        return False
    try:
        me = await ctx.wrapper.me(uid)
    except (WrapperError, WrapperUnavailable, WrapperContractError):
        log.exception("me_after_payment_failed", extra={"tg_user": uid})
        me = None
    if me is not None and not me.consented:
        log.info("payment_result_not_sent_opted_out", extra={"tg_user": uid, "payment_id": status.payment_id})
        return True
    if status.status == "canceled":
        await say(ctx, bot, uid, texts.payment_canceled(status.cancellation_reason))
    else:
        await say(ctx, bot, uid, texts.payment_success(me))
    return True


class PaymentWatchers:
    def __init__(
        self,
        ctx: AppContext,
        bot: Bot,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        delays: Callable[[], Iterator[float]] = backoff_delays,
    ) -> None:
        self._ctx = ctx
        self._bot = bot
        self._sleep = sleep
        self._delays = delays
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def active(self, payment_id: str) -> bool:
        return payment_id in self._tasks

    def watch(self, uid: int, payment_id: str) -> bool:
        """Start polling in the background; False when already watching or at capacity (logged)."""
        if payment_id in self._tasks:
            return False
        if len(self._tasks) >= MAX_WATCHERS:
            log.error("payment_watch_capacity", extra={"tg_user": uid, "payment_id": payment_id})
            return False
        task = asyncio.create_task(self._run(uid, payment_id), name=f"watch:{payment_id}")
        self._tasks[payment_id] = task
        task.add_done_callback(lambda t, pid=payment_id: self._done(pid, t))
        return True

    def stop(self, payment_id: str) -> None:
        task = self._tasks.get(payment_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _done(self, payment_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(payment_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("payment_watch_crashed", exc_info=task.exception(), extra={"payment_id": payment_id})

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, uid: int, payment_id: str) -> None:
        ctx, bot = self._ctx, self._bot
        failures = 0
        last: schemas.PaymentStatus | None = None
        for delay in self._delays():
            await self._sleep(delay)
            try:
                last = await ctx.wrapper.payment_status(uid, payment_id)
            except WrapperError as exc:
                if exc.code == "rate_limited":
                    await self._sleep(min(exc.retry_after_s or 5, 60))
                    continue
                if exc.code in ("payment_not_found", "telegram_not_linked", "tenant_inactive"):
                    await fail(ctx, bot, uid, exc, "payment")
                    return
                failures += 1
            except (WrapperUnavailable, WrapperContractError):
                failures += 1
            else:
                failures = 0
                if await announce(ctx, bot, uid, last):
                    return
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("payment_watch_gave_up", extra={"tg_user": uid, "payment_id": payment_id})
                await say(
                    ctx, bot, uid,
                    "Не удалось проверить платёж: сервис недоступен. Попробуйте позже кнопкой ниже.",
                    keyboards.check_payment(payment_id),
                )
                return
        credit_delayed = last is not None and last.status == "succeeded"
        log.warning(
            "payment_watch_timeout",
            extra={"tg_user": uid, "payment_id": payment_id, "paid_not_credited": credit_delayed},
        )
        await say(
            ctx, bot, uid,
            texts.payment_credit_delayed() if credit_delayed else texts.payment_pending_timeout(),
            keyboards.check_payment(payment_id),
        )
