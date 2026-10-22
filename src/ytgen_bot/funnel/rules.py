"""Tariff choice: a deterministic script over the PDF rule table (section 5), top to bottom, FIRST MATCH WINS.

The result is a ROLE (entry / base / pro / ultra, by price rank among the wrapper's tariffs); the tariff, its price and
every limit are then read from the wrapper. The AI never takes part in this.

KNOWN DEFECT OF THE SOURCE TABLE (reported to the owner, left as written): rule 6 (ULTRA) can never be reached, because
every answer set that satisfies it also satisfies rule 5 (`s2_high or s1_team`) which sits above it. The order below is the
PDF's order; `tests/test_funnel_rules.py` pins that fact so changing it is a deliberate, visible decision.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable

from .model import Role


@dataclasses.dataclass(frozen=True)
class Answers:
    stage: str | None  # s1_*
    volume: str | None  # s2_*
    goal: str | None  # n1_*


Rule = tuple[str, Callable[[Answers], bool], Role]

RULES: tuple[Rule, ...] = (
    (
        "plan_or_start_and_low_or_unknown",
        lambda a: (a.stage == "s1_plan" or a.goal == "n1_start") and a.volume in ("s2_low", "s2_unk"),
        Role.ENTRY,
    ),
    ("low_volume", lambda a: a.volume == "s2_low", Role.BASE),
    ("mid_volume_one_channel", lambda a: a.volume == "s2_mid" and a.stage == "s1_one", Role.BASE),
    (
        "mid_volume_multi_or_scale",
        lambda a: a.volume == "s2_mid" and (a.stage == "s1_multi" or a.goal == "n1_scale"),
        Role.PRO,
    ),
    ("high_volume_or_team", lambda a: a.volume == "s2_high" or a.stage == "s1_team", Role.PRO),
    (
        "team_scale_or_high_more",
        lambda a: (a.stage == "s1_team" and a.goal == "n1_scale") or (a.volume == "s2_high" and a.goal == "n1_more"),
        Role.ULTRA,
    ),
)
DEFAULT_ROLE = Role.BASE  # "any other set of answers": a neutral start


def choose_role(answers: Answers) -> tuple[Role, str]:
    """(role, name of the rule that matched). Top to bottom, first match."""
    for name, predicate, role in RULES:
        if predicate(answers):
            return role, name
    return DEFAULT_ROLE, "default"
