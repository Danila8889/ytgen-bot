"""A controllable clock for the funnel (reminder schedule, hourly AI limit, retention)."""
from __future__ import annotations

import datetime as dt

START = dt.datetime(2026, 10, 22, 10, 0, tzinfo=dt.UTC)


class FakeClock:
    def __init__(self, start: dt.datetime = START) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kw: float) -> dt.datetime:
        self.now += dt.timedelta(**kw)
        return self.now
