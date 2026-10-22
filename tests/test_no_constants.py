"""No price, limit, percentage or period lives in the bot: everything shown comes from a wrapper response."""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ytgen_bot"
USER_FACING = [SRC / "texts.py", SRC / "keyboards.py", SRC / "errors_ru.py", *sorted((SRC / "handlers").glob("*.py"))]
# Structural numbers only: indexes, "a day is 24 h", "a minute is 60 s", the email length, HTTP >= 500.
ALLOWED_NUMBERS = {0, 1, 2, 24, 60, 254, 500}
MONEY_OR_PERCENT = re.compile(r"\d\s*(₽|руб|RUB|%|рубл)", re.IGNORECASE)


def numbers(path: pathlib.Path) -> set[int | float]:
    tree = ast.parse(path.read_text())
    return {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    }


def test_user_facing_modules_contain_no_business_numbers():
    offenders = {p.name: sorted(numbers(p) - ALLOWED_NUMBERS) for p in USER_FACING}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_literal_amount_or_percentage_in_any_source_string():
    hits = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and MONEY_OR_PERCENT.search(node.value):
                hits.append((path.name, node.value[:60]))
    assert hits == []


def test_known_business_values_do_not_appear_in_the_source():
    """The wrapper's current numbers (reward 7 %, hold 14 days, minimum 100 RUB, prices) must not be copied."""
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for needle in ("12900", "12 900", "4250", "4 250", "297", "903", "7%", "7 %", "14 дн"):
        assert needle not in text, needle


def test_every_figure_on_screen_follows_the_api_values(monkeypatch):
    """Rendering with unusual values shows exactly those values (so nothing is a baked-in constant)."""
    from ytgen_bot import schemas, texts
    from ytgen_bot.formatting import rub

    tariff = schemas.Tariff(id=1, name="Икс", price_rub=13579, period_days=9)
    assert rub(13579) in texts.tariff_block(tariff) and "9 дн." in texts.tariff_block(tariff)
    info = schemas.ReferralInfo(
        code="c", start_parameter="ref_c", deep_link="https://t.me/x?start=ref_c", reward_percent=23,
        hold_days=5, min_withdrawal_rub=333, is_referred=False,
        counts=schemas.ReferralCounts(opens=0, attributed=0, paid=0),
        balance=schemas.ReferralBalance(pending_rub=0, available_rub=0, requested_rub=0, paid_out_rub=0, owed_rub=0),
    )
    screen = texts.referral_screen(info, "@s")
    assert "23%" in screen and "5 дн." in screen and rub(333) in screen
