"""The PDF section 5 rule table: first match wins, roles not names, and the known shadowed ULTRA rule is pinned."""
from __future__ import annotations

import itertools

import pytest

from ytgen_bot.funnel.model import Role
from ytgen_bot.funnel.rules import RULES, Answers, choose_role

STAGES = ["s1_plan", "s1_one", "s1_multi", "s1_team"]
VOLUMES = ["s2_low", "s2_mid", "s2_high", "s2_unk"]
GOALS = ["n1_more", "n1_scale", "n1_start", "n1_time", "n1_lang"]


@pytest.mark.parametrize(
    "stage,volume,goal,role,rule",
    [
        ("s1_plan", "s2_low", "n1_more", Role.ENTRY, "plan_or_start_and_low_or_unknown"),
        ("s1_one", "s2_unk", "n1_start", Role.ENTRY, "plan_or_start_and_low_or_unknown"),
        ("s1_plan", "s2_unk", "n1_time", Role.ENTRY, "plan_or_start_and_low_or_unknown"),
        ("s1_one", "s2_low", "n1_more", Role.BASE, "low_volume"),
        ("s1_team", "s2_low", "n1_scale", Role.BASE, "low_volume"),
        ("s1_one", "s2_mid", "n1_more", Role.BASE, "mid_volume_one_channel"),
        ("s1_multi", "s2_mid", "n1_more", Role.PRO, "mid_volume_multi_or_scale"),
        ("s1_plan", "s2_mid", "n1_scale", Role.PRO, "mid_volume_multi_or_scale"),
        ("s1_one", "s2_high", "n1_more", Role.PRO, "high_volume_or_team"),
        ("s1_team", "s2_unk", "n1_time", Role.PRO, "high_volume_or_team"),
        ("s1_plan", "s2_mid", "n1_time", Role.BASE, "default"),
        ("s1_one", "s2_unk", "n1_more", Role.BASE, "default"),
        ("s1_multi", "s2_unk", "n1_lang", Role.BASE, "default"),
    ],
)
def test_first_match_wins(stage, volume, goal, role, rule):
    assert choose_role(Answers(stage, volume, goal)) == (role, rule)


def test_missing_answers_fall_to_the_neutral_default():
    assert choose_role(Answers(None, None, None)) == (Role.BASE, "default")


def test_rule_order_is_the_pdf_order():
    assert [role for _, _, role in RULES] == [Role.ENTRY, Role.BASE, Role.BASE, Role.PRO, Role.PRO, Role.ULTRA]


def test_KNOWN_DEFECT_the_ultra_rule_is_shadowed_by_the_pro_rule_above_it():
    """PINNED, not endorsed (OPEN QUESTION in the task report): every answer set matching rule 6 also matches rule 5
    (`s2_high or s1_team`), so with 'first match wins' ULTRA is never recommended. If the owner reorders the table,
    change this test on purpose."""
    outcomes = {choose_role(Answers(s, v, g))[0] for s, v, g in itertools.product(STAGES, VOLUMES, GOALS)}
    assert Role.ULTRA not in outcomes and outcomes == {Role.ENTRY, Role.BASE, Role.PRO}
    ultra_rule = RULES[-1][1]
    shadow = RULES[-2][1]
    for s, v, g in itertools.product(STAGES, VOLUMES, GOALS):
        if ultra_rule(Answers(s, v, g)):
            assert shadow(Answers(s, v, g)), (s, v, g)


def test_the_table_has_no_money_and_no_tariff_names():
    import inspect

    from ytgen_bot.funnel import rules

    source = inspect.getsource(rules)
    for needle in ("Lite", "Base", "Pro ", "Ultra ", "₽", "руб"):
        assert needle not in source.replace("Ultra rule", "").replace("ULTRA", "")
