"""Small presentation helpers (HTML-safe, Russian)."""
from __future__ import annotations

import datetime as dt
import html


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


NBSP = "\u00a0"


def rub(amount: int) -> str:
    """Whole rubles with a thousands separator and the currency sign, joined by non-breaking spaces."""
    return f"{amount:,}".replace(",", NBSP) + f"{NBSP}₽"


def when(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%d.%m.%Y %H:%M UTC")


def wait(seconds: int) -> str:
    """A retry delay a person can read: `45 с`, `3 мин`, `2 ч 5 мин`."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes = -(-seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
