"""The bot's own throttling, in front of the wrapper's: token bucket, action guard, and the middleware."""
from __future__ import annotations

from tests.conftest import UID, Harness
from ytgen_bot import callbacks, texts
from ytgen_bot.ratelimit import ActionGuard, TokenBucket


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_allows_a_burst_then_refuses_then_refills():
    clock = Clock()
    bucket = TokenBucket(capacity=3, refill_per_s=1.0, clock=clock)
    assert [bucket.check(1).allowed for _ in range(3)] == [True, True, True]
    refused = bucket.check(1)
    assert not refused.allowed and refused.retry_after_s == 1
    clock.now += 1.0
    assert bucket.check(1).allowed
    assert not bucket.check(1).allowed


def test_buckets_are_per_user():
    bucket = TokenBucket(capacity=1, refill_per_s=0.1, clock=Clock())
    assert bucket.check(1).allowed and not bucket.check(1).allowed
    assert bucket.check(2).allowed


def test_notice_is_rate_limited_itself():
    clock = Clock()
    bucket = TokenBucket(capacity=1, refill_per_s=0.01, clock=clock)
    bucket.check(1)
    first, second = bucket.check(1), bucket.check(1)
    assert first.notify and not second.notify
    clock.now += 6
    assert bucket.check(1).notify


def test_idle_buckets_are_pruned():
    clock = Clock()
    bucket = TokenBucket(clock=clock)
    for uid in range(300):
        bucket.check(uid)
    clock.now += 4000
    for _ in range(300):
        bucket.check(10_000)
    assert len(bucket) < 50


def test_action_guard_blocks_in_flight_and_cooldown():
    clock = Clock()
    guard = ActionGuard(cooldown_s=3.0, clock=clock)
    assert guard.acquire("a")
    assert not guard.acquire("a")  # in flight
    guard.release("a")
    assert not guard.acquire("a")  # cooldown
    clock.now += 3.1
    assert guard.acquire("a")
    assert guard.acquire("b")


def test_action_guard_without_cooldown():
    guard = ActionGuard(cooldown_s=3.0, clock=Clock())
    with guard.hold("k", cooldown=False) as got:
        assert got
    with guard.hold("k", cooldown=False) as again:
        assert again


async def test_flood_is_cut_off_before_it_reaches_the_wrapper(linked: Harness):
    h = linked
    h.ctx.limiter = TokenBucket(capacity=3, refill_per_s=0.01, clock=Clock())
    for _ in range(10):
        await h.say("/tariffs")
    assert len(h.fake.calls("GET", "/tariffs")) == 3
    assert sum("Слишком часто" in t for t in h.texts) == 1  # one notice, not one per message


async def test_flooded_button_gets_an_answer_not_silence(linked: Harness):
    h = linked
    h.ctx.limiter = TokenBucket(capacity=1, refill_per_s=0.01, clock=Clock())
    await h.press(callbacks.menu("help"))
    await h.press(callbacks.menu("help"))
    assert any("Слишком часто" in (a.text or "") for a in h.session.answers())


async def test_one_users_flood_does_not_throttle_another(linked: Harness):
    h = linked
    h.ctx.limiter = TokenBucket(capacity=1, refill_per_s=0.01, clock=Clock())
    await h.say("/help")
    await h.say("/help")
    h.fake.consent(2002)
    await h.say("/help", uid=2002)
    assert h.session.sent(2002)


async def test_wrapper_429_is_shown_with_the_retry_time(linked: Harness):
    h = linked
    h.fake.queue_error("GET", "/tariffs", 429, "rate_limited", bucket="bot_call", retry_after_s=42)
    await h.say("/tariffs")
    assert "42 с" in h.last


async def test_overlong_input_is_rejected_early(linked: Harness):
    h = linked
    await h.say("x" * 5000)
    assert h.last == texts.TOO_LONG
    assert h.fake.requests == []


async def test_non_text_message_gets_a_hint_not_a_crash(linked: Harness):
    import datetime as dt

    from aiogram.types import Chat, Message, Update, User

    h = linked
    update = Update(update_id=99, message=Message(
        message_id=1, date=dt.datetime.now(dt.UTC), chat=Chat(id=UID, type="private"),
        from_user=User(id=UID, is_bot=False, first_name="T"),
    ))
    await h.dp.feed_update(h.bot, update)
    assert h.last == texts.UNKNOWN_MESSAGE
