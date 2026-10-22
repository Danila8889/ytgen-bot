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
SOURCES = ("yt", "tg", "site")  # reserved /start source tokens (first-touch attribution)

# The funnel's button codes (PDF spec sections 3-9). One closed set: anything else is "button outdated".
FUNNEL_CODES = (
    "intro_how", "spin_start", "examples", "ask_free", "handoff_manual", "back_start", "doubt_open", "resume",
    "back", "back_close",
    "s1_plan", "s1_one", "s1_multi", "s1_team",
    "s2_low", "s2_mid", "s2_high", "s2_unk", "s3_skip",
    "p1_edit", "p1_script", "p1_voice", "p1_tools", "p1_start", "p1_other",
    "i1_slow", "i1_noscale", "i1_tired", "i1_cost", "i1_none",
    "n1_more", "n1_scale", "n1_start", "n1_time", "n1_lang",
    "show_details", "details_next", "compare", "try_lite", "pay_main", "go_pay",
    "obj_price", "obj_quality", "obj_hard", "obj_ban", "obj_gpt", "obj_time", "obj_roi", "obj_refund", "obj_think",
    "q_visual", "q_voice", "q_template", "h_setup", "h_yt", "g_script", "g_video", "g_all", "g_text",
    "remind_ok", "end_polite", "pay_done", "pay_not_yet", "unsub",
)

_ID = r"(\d{1,9})"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # cn = consent; cn:<referral code> or cn.<source token> (a /start payload is held in the button until consent)
    ("consent", re.compile(rf"^cn(?::([A-Za-z0-9_-]{{1,60}})|\.({'|'.join(SOURCES)}))?$")),
    ("funnel", re.compile(rf"^f:({'|'.join(FUNNEL_CODES)})$")),
    ("reminders", re.compile(r"^rm:(yes|no)$")),
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


def consent(ref_code: str | None = None, source: str | None = None) -> str:
    if ref_code:
        return _checked(f"cn:{ref_code}")
    return _checked(f"cn.{source}" if source else "cn")


def funnel(code: str) -> str:
    return _checked(f"f:{code}")


def reminders(answer: str) -> str:
    return _checked(f"rm:{answer}")


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
