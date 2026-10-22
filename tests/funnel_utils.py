"""Helpers for driving the funnel through the real dispatcher (callbacks and text, like a Telegram client would)."""
from __future__ import annotations

from tests.conftest import UID, Harness
from tests.fake_wrapper import funnel_tariffs
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks
from ytgen_bot.ratelimit import ActionGuard, TokenBucket


def relax(h: Harness) -> Harness:
    """The per-user throttle is its own feature (tests/test_ratelimit.py); funnel scenarios press many buttons."""
    h.ctx.limiter = TokenBucket(capacity=100_000, refill_per_s=100_000)
    h.ctx.guard = ActionGuard(cooldown_s=0.0)  # the double-tap cooldown is real time; scenarios act at once
    return h


async def press(h: Harness, *codes: str, uid: int = UID) -> None:
    for code in codes:
        await h.press(callbacks.funnel(code), uid=uid)


def codes_of(h: Harness) -> list[str]:
    """The funnel callback codes on the last message's keyboard (url buttons are skipped)."""
    return [b.callback_data.removeprefix("f:") for b in buttons(h.session.last_markup()) if b.callback_data]


def labels_of(h: Harness) -> list[str]:
    return [b.text for b in buttons(h.session.last_markup())]


async def join(h: Harness, *, source: str | None = None, reminders: bool | None = True) -> Harness:
    """A new, unlinked person consents (optionally via a source deep link) and answers the reminders opt-in."""
    relax(h)
    h.fake.tariffs = funnel_tariffs()
    await h.say(f"/start {source}" if source else "/start")
    await h.press(callbacks.consent(source=source))
    if reminders is not None:
        await h.press(callbacks.reminders("yes" if reminders else "no"))
    return h


async def run_spin(h: Harness, stage: str, volume: str, pain: str, impact: str, goal: str) -> None:
    await press(h, "spin_start", stage, volume, "s3_skip", pain, impact, goal)
