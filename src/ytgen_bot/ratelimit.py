"""In-process, per-Telegram-user throttling that sits in front of the wrapper's own (DB-backed) limits.

* `TokenBucket`: a burst allowance that refills steadily; keeps a flooding user from turning into
  wrapper calls (the wrapper allows 60 calls / minute / user and answers 429 beyond that).
* `ActionGuard`: one in-flight call per (user, action) plus a short cooldown afterwards: the double-tap
  debounce for state-changing buttons (checkout, withdrawal).

Both are memory-only by design (the bot holds no state across restarts); idle entries are pruned.
"""
from __future__ import annotations

import contextlib
import dataclasses
import math
import time
from collections.abc import Callable, Hashable, Iterator

# 6 quick actions, then one every two seconds: well inside the wrapper's 60 calls / minute even when an
# action costs two wrapper calls.
DEFAULT_CAPACITY = 6
DEFAULT_REFILL_PER_S = 0.5
NOTICE_EVERY_S = 5.0
_PRUNE_EVERY = 256
_IDLE_S = 3600.0


@dataclasses.dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: int = 0
    notify: bool = False  # tell the user (at most once per NOTICE_EVERY_S) instead of staying silent


@dataclasses.dataclass
class _Bucket:
    tokens: float
    updated: float
    last_notice: float = -1e18


class TokenBucket:
    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_s: float = DEFAULT_REFILL_PER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = float(capacity)
        self._refill = refill_per_s
        self._clock = clock
        self._buckets: dict[int, _Bucket] = {}
        self._calls = 0

    def check(self, user_id: int) -> Decision:
        now = self._clock()
        self._calls += 1
        if self._calls % _PRUNE_EVERY == 0:
            self._prune(now)
        bucket = self._buckets.get(user_id)
        if bucket is None:
            bucket = self._buckets[user_id] = _Bucket(tokens=self._capacity, updated=now)
        bucket.tokens = min(self._capacity, bucket.tokens + (now - bucket.updated) * self._refill)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return Decision(True)
        wait = max(1, math.ceil((1.0 - bucket.tokens) / self._refill))
        notify = now - bucket.last_notice >= NOTICE_EVERY_S
        if notify:
            bucket.last_notice = now
        return Decision(False, retry_after_s=wait, notify=notify)

    def _prune(self, now: float) -> None:
        for uid in [u for u, b in self._buckets.items() if now - b.updated > _IDLE_S]:
            del self._buckets[uid]

    def __len__(self) -> int:
        return len(self._buckets)


class ActionGuard:
    """`acquire(key)` is False while the same action is in flight or finished less than `cooldown_s` ago."""

    def __init__(self, cooldown_s: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown_s
        self._clock = clock
        self._in_flight: set[Hashable] = set()
        self._released_at: dict[Hashable, float] = {}

    def acquire(self, key: Hashable) -> bool:
        now = self._clock()
        if len(self._released_at) > _PRUNE_EVERY:
            self._released_at = {k: t for k, t in self._released_at.items() if now - t < self._cooldown}
        if key in self._in_flight:
            return False
        released = self._released_at.get(key)
        if released is not None and now - released < self._cooldown:
            return False
        self._in_flight.add(key)
        return True

    def release(self, key: Hashable, *, cooldown: bool = True) -> None:
        self._in_flight.discard(key)
        if cooldown:
            self._released_at[key] = self._clock()

    @contextlib.contextmanager
    def hold(self, key: Hashable, *, cooldown: bool = True) -> Iterator[bool]:
        """`with guard.hold(key) as acquired:` - releases (and, with `cooldown`, starts it) only if acquired."""
        acquired = self.acquire(key)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(key, cooldown=cooldown)
