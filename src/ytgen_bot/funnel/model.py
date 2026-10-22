"""Funnel vocabulary: statuses, sources, tariff roles and the `Lead` row."""
from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum


class Status(StrEnum):
    NEW = "new"
    IN_SPIN = "in_spin"
    RECOMMENDED = "recommended"
    LINK_SENT = "link_sent"
    HANDOFF = "handoff"
    PAID = "paid"
    COLD = "cold"
    BLOCKED = "blocked"
    STOPPED = "stopped"  # tombstone after /stop: tg id + this status + timestamp, nothing else


class Role(StrEnum):
    """Tariff roles by rank among the wrapper's public tariffs, never by name."""

    ENTRY = "entry"  # the cheapest non-monthly tariff
    BASE = "base"  # monthly tariffs by ascending price
    PRO = "pro"
    ULTRA = "ultra"


CARD_CLIP = 300  # characters of a free-text answer shown in the manager card (keeps it inside Telegram's limit)
MAX_PAID_NOTE = 300  # characters of a manager's /paid note that are kept
SOURCES = ("yt", "tg", "site")

# Statuses in which the sales script is still allowed to talk to the person.
SELLING = (Status.NEW, Status.IN_SPIN, Status.RECOMMENDED, Status.LINK_SENT, Status.COLD)


@dataclasses.dataclass(frozen=True)
class Lead:
    tg_id: int
    username: str | None
    first_name: str | None
    source: str | None
    status: str
    step: str | None
    stage: str | None
    volume: str | None
    niche_text: str | None
    pain: str | None
    pain_text: str | None
    impact: str | None
    goal: str | None
    rec_role: str | None
    rec_tariff_id: int | None
    handoff_reason: str | None
    handoff_at: dt.datetime | None
    handoff_priority: str | None
    remind_ok: bool
    reminders_opt_in: bool | None
    followups_sent: int
    answered_followup: bool
    last_followup_at: dt.datetime | None
    unsub: bool
    paid_tariff: str | None
    paid_at: dt.datetime | None
    paid_by_manager: bool
    paid_announced: bool
    pay_check_sent: bool
    notes: str | None
    last_activity_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime
    stopped_at: dt.datetime | None

    @property
    def is_tombstone(self) -> bool:
        return self.status == Status.STOPPED
