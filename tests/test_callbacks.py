"""Callback data: round trips, size limit, and rejection of forged / malformed values."""
from __future__ import annotations

import pytest

from ytgen_bot import callbacks as cb


def test_round_trips():
    assert cb.parse(cb.consent()) == cb.Action("consent", (None,))
    assert cb.parse(cb.consent("ABC_def-1")) == cb.Action("consent", ("ABC_def-1",))
    assert cb.parse(cb.tariff(7)) == cb.Action("tariff", ("7",))
    assert cb.parse(cb.pay(7, 1234)) == cb.Action("pay", ("7", "1234", None))
    assert cb.parse(cb.pay(7, 1234, "SAVE-10_X")) == cb.Action("pay", ("7", "1234", "SAVE-10_X"))
    assert cb.parse(cb.check("2c0a7fa5-000f-5000-8000-1c3e2b3a4d5e")).kind == "check"
    assert cb.parse(cb.withdraw_ok(500)) == cb.Action("withdraw_ok", ("500",))
    for target in cb.MENU_TARGETS:
        assert cb.parse(cb.menu(target)).kind == "menu"


def test_worst_case_fits_in_64_bytes():
    assert len(cb.pay(999_999_999, 999_999_999, "A" * 32).encode()) <= 64
    assert len(cb.consent("r" * 60).encode()) <= 64
    assert len(cb.check("p" * 60).encode()) <= 64


@pytest.mark.parametrize(
    "data",
    [
        None, "", "x", "t:", "t:abc", "t:-1", "t:1234567890", "t:1 ", "pay:1", "pay:1:2:lower", "pay:1:2:" + "A" * 33,
        "chk:", "chk:a b", "chk:" + "a" * 61, "cn:" + "a" * 61, "cn:bad code", "m:admin", "wd:drop", "wdok:x",
        "t:1;DROP", "t:1\n", "x" * 65, "pay:1:2:A:B",
    ],
)
def test_forged_or_malformed_data_is_rejected(data):
    assert cb.parse(data) is None
