"""Callback data: built and parsed in one place, strictly validated, always within Telegram's 64 bytes.

Buttons that must keep working after a restart carry everything they need in the data itself (payment id,
tariff id, expected amount, promo code, referral code), because the bot keeps nothing between runs.
"""
from __future__ import annotations

import dataclasses
import re

MAX_BYTES = 64

REF_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
PROMO_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")
PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
MENU_TARGETS = ("tariffs", "referral", "status", "help", "link")

_ID = r"(\d{1,9})"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("consent", re.compile(r"^cn(?::([A-Za-z0-9_-]{1,60}))?$")),
    ("menu", re.compile(rf"^m:({'|'.join(MENU_TARGETS)})$")),
    ("tariff", re.compile(rf"^t:{_ID}$")),
    ("link", re.compile(rf"^lk:{_ID}$")),  # lk:0 = just link, lk:<id> = link, then continue with the tariff
    ("promo", re.compile(rf"^pr:{_ID}$")),
    ("quote", re.compile(rf"^q:{_ID}$")),
    ("pay", re.compile(rf"^pay:{_ID}:{_ID}(?::([A-Z0-9_-]{{1,32}}))?$")),
    ("check", re.compile(r"^chk:([A-Za-z0-9_-]{1,60})$")),
    ("withdraw", re.compile(r"^wd:(start|cancel)$")),
    ("withdraw_ok", re.compile(rf"^wdok:{_ID}$")),
)


@dataclasses.dataclass(frozen=True)
class Action:
    kind: str
    args: tuple[str | None, ...] = ()


def parse(data: str | None) -> Action | None:
    """None for anything that is not exactly one of our buttons (stale, forged, malformed)."""
    if not data or len(data.encode("utf-8")) > MAX_BYTES:
        return None
    for kind, pattern in _PATTERNS:
        match = pattern.fullmatch(data)
        if match:
            return Action(kind, match.groups())
    return None


def _checked(data: str) -> str:
    assert len(data.encode("utf-8")) <= MAX_BYTES, data
    assert parse(data) is not None, data
    return data


def consent(ref_code: str | None = None) -> str:
    return _checked(f"cn:{ref_code}" if ref_code else "cn")


def menu(target: str) -> str:
    return _checked(f"m:{target}")


def tariff(tariff_id: int) -> str:
    return _checked(f"t:{tariff_id}")


def link(tariff_id: int = 0) -> str:
    return _checked(f"lk:{tariff_id}")


def promo(tariff_id: int) -> str:
    return _checked(f"pr:{tariff_id}")


def quote(tariff_id: int) -> str:
    return _checked(f"q:{tariff_id}")


def pay(tariff_id: int, expected_rub: int, promo_code: str | None = None) -> str:
    tail = f":{promo_code}" if promo_code else ""
    return _checked(f"pay:{tariff_id}:{expected_rub}{tail}")


def check(payment_id: str) -> str:
    return _checked(f"chk:{payment_id}")


def withdraw(action: str) -> str:
    return _checked(f"wd:{action}")


def withdraw_ok(amount_rub: int) -> str:
    return _checked(f"wdok:{amount_rub}")
