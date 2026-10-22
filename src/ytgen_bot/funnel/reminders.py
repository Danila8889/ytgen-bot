"""Reminder schedule (PDF section 9) as a pure function, plus the due-time loop.

Rules (all of F1-F3 REQUIRE `reminders_opt_in`):
  F1  24 h after the last activity, once, for a lead who abandoned SPIN (`in_spin`) or did not respond after the
      recommendation (`recommended`).
  F2  3 days after F1, F3 7 days after F2: only for a lead who pressed `remind_ok` or answered F1 (any activity after it).
      Never earlier than 24 h after their last activity (a person who has just been active is not pinged at once).
  M5.3 (the "did the payment work?" check): 30 minutes after `go_pay`, once, for a `link_sent` lead who opted in.
Any activity, hand-off, paid, unsub, /stop or a blocked bot cancels what is pending (the schedule is recomputed from the
lead after every change). Delivery is AT-MOST-ONCE: a row is claimed before it is sent, so a crash between the claim and
the send loses that one message instead of sending it twice.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiogram import Bot

from ..db.store import PgStore, StoreError
from .model import Lead, Status

if TYPE_CHECKING:  # pragma: no cover
    from ..context import AppContext
    from .flow import Funnel

log = logging.getLogger(__name__)

F1_AFTER = dt.timedelta(hours=24)
F2_AFTER_F1 = dt.timedelta(days=3)
F3_AFTER_F2 = dt.timedelta(days=7)
QUIET_AFTER_ACTIVITY = dt.timedelta(hours=24)
M53_AFTER_GO_PAY = dt.timedelta(minutes=30)

TICK_S = 30.0
BATCH = 20
SEND_PACE_S = 0.25  # <= 4 sends per second, far below Telegram's ~30 messages/second


def next_followup(lead: Lead) -> tuple[str, dt.datetime] | None:
    """The next scheduled message for this lead, or None. Pure: depends on the lead row only."""
    if lead.reminders_opt_in is not True or lead.unsub or lead.is_tombstone:
        return None
    if lead.status == Status.LINK_SENT:
        if lead.pay_check_sent:
            return None
        return "m53", lead.last_activity_at + M53_AFTER_GO_PAY
    if lead.status not in (Status.IN_SPIN, Status.RECOMMENDED):
        return None
    sent = lead.followups_sent
    if sent == 0:
        return "f1", lead.last_activity_at + F1_AFTER
    if sent >= 3 or not (lead.remind_ok or lead.answered_followup) or lead.last_followup_at is None:
        return None
    gap, kind = (F2_AFTER_F1, "f2") if sent == 1 else (F3_AFTER_F2, "f3")
    return kind, max(lead.last_followup_at + gap, lead.last_activity_at + QUIET_AFTER_ACTIVITY)


async def sync_schedule(store: PgStore, uid: int) -> None:
    """Recompute and store the single pending follow-up of this user (cancelling whatever was pending)."""
    lead = await store.get_lead(uid)
    plan = next_followup(lead) if lead is not None else None
    await store.replace_followup(uid, plan[0] if plan else None, plan[1] if plan else None)


class ReminderLoop:
    """Background task: every TICK_S, claim due rows and send them. All state is in Postgres, so a restart loses nothing
    except a message that was claimed but not yet sent."""

    def __init__(
        self,
        funnel: Funnel,
        ctx: AppContext,
        bot: Bot,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._funnel = funnel
        self._ctx = ctx
        self._bot = bot
        self._sleep = sleep

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except StoreError:
                pass  # already logged at ERROR by the store; try again next tick
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reminder_tick_crashed")
            await self._sleep(TICK_S)

    async def tick(self) -> int:
        """One pass; returns the number of messages sent (for tests)."""
        store = self._funnel.store
        now = self._funnel.clock()
        sent = 0
        for item in await store.claim_due_followups(now, BATCH):
            if await self._funnel.deliver_followup(self._ctx, self._bot, item):
                sent += 1
                await self._sleep(SEND_PACE_S)
        return sent
