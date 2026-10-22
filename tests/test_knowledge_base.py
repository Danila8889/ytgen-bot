"""The static knowledge base: no tariff money or limits, sources everywhere, and the owner's removals stay removed."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.fake_wrapper import funnel_tariffs
from ytgen_bot import knowledge
from ytgen_bot.funnel import texts as funnel_texts
from ytgen_bot.funnel.tariffs import parse_public_tariffs

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ytgen_bot"
KB_FILE = pathlib.Path(knowledge.PATH)

# Money / limit-looking figures: a number glued to a currency, a per-day/per-window count, a period, or a unit of work.
LIMIT_LIKE = re.compile(
    r"(\d[\d\s ]*\s*(₽|руб|\$|дн\.|дней|дня|сутк|клип|картин|сток)|\d\s*(видео|видеоролик)|"
    r"в\s+(день|сутки)|за\s+\d+\s*(дн|нед|мес))",
    re.IGNORECASE,
)


def kb():
    return knowledge.load("@support_handle")


def test_every_fact_has_a_nonempty_text_and_a_source():
    facts = kb().facts
    assert len(facts) >= 20
    assert all(f.text.strip() and f.source.strip() for f in facts)
    assert len({f.id for f in facts}) == len(facts)


def test_the_static_knowledge_base_contains_no_tariff_price_or_limit():
    """Tariff figures come only from the wrapper. This file may carry numbers only for NON-tariff facts."""
    text = KB_FILE.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    offenders = [m.group(0) for m in LIMIT_LIKE.finditer(" ".join(f.text for f in kb().facts))]
    assert offenders == [], offenders
    assert "₽" not in body and "$" not in body and "руб" not in body.lower().replace("рубл", "").replace("руб.", "")


def test_no_number_of_any_current_tariff_appears_in_the_knowledge_base_text():
    numbers = set()
    for row in funnel_tariffs():
        numbers.add(row["price_rub"])
        numbers.update(v for v in row["capacity"].values() if isinstance(v, int))
        numbers.update(v for k, v in row.items() if isinstance(v, int) and k != "id")
    text = " ".join(f.text for f in kb().facts)
    tokens = set(re.findall(r"(?<![\w.])\d+(?![\w.])", text))
    big = {str(n) for n in numbers if n >= 20}  # small figures (5, 7, ...) legitimately collide with non-tariff facts
    assert not tokens & big, tokens & big


def test_the_only_numbers_in_the_static_knowledge_base_are_the_known_non_tariff_facts():
    allowed = {"4", "12", "5", "1", "3.1", "3", "15"}  # dubbing 4-of-12, tracks per channel, 1-5 links, versions, handle/link
    tokens = set(re.findall(r"\d+(?:\.\d+)?", " ".join(f.text for f in kb().facts)))
    assert tokens <= allowed, tokens - allowed


@pytest.mark.parametrize(
    "needle",
    [
        "компьютерн", "computer vision", "проверяет кадр", "перегенерируются автоматически", "вспомогательн",
        "$", "≈", "примерно 66", "togn15", "Lite Start", "можно повысить", "понизить", "ориентировочн", "Менеджер добавит",
        "осталось мест",
    ],
)
def test_the_owners_removals_stay_removed_from_the_knowledge_base(needle):
    text = " ".join(f.text for f in kb().facts).lower()
    assert needle.lower() not in text


def test_the_facts_the_owner_asked_to_keep_are_there():
    text = " ".join(f.text for f in kb().facts)
    assert "Несколько каналов доступны на любом тарифе." in text  # the site FAQ sentence, no numbers
    assert "Любую сцену можно перегенерировать вручную в сервисе, по промту." in text
    assert "Автопродления нет" in text
    assert "доступ в группу уточняется у менеджера" in text.lower()
    assert "в разработке" in text and "Моушн-графика" in text


def test_the_support_contact_is_rendered_from_the_env_value_never_hardcoded():
    assert "@support_handle" in kb().fact("contacts") and "@support_handle" in kb().fact("support_group")
    assert "{SUPPORT_CONTACT}" not in kb().facts_block()
    assert "@togn15" not in KB_FILE.read_text(encoding="utf-8")


def test_examples_are_https_links():
    assert len(kb().examples) == 4 and all(e.url.startswith("https://") for e in kb().examples)


def test_funnel_texts_never_hardcode_the_manager_handle_or_the_removed_claims():
    banned = ("@togn15", "togn15", "компьютерн", "вспомогательн", "ориентировочн", "≈", "ytgenerator.ru/auth", "Менеджер добавит")
    for path in (SRC / "funnel" / "texts.py", SRC / "funnel" / "render.py", SRC / "ai" / "prompt.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "texts.py":  # the module docstring describes the rules by naming them
            text = text.split('"""', 2)[2]
        for needle in banned:
            if path.name == "prompt.py" and needle in ("компьютерн", "вспомогательн"):
                continue  # the prompt names them in order to FORBID them
            assert needle not in text, (path.name, needle)


def test_funnel_texts_contain_no_figures_next_to_units_literals():
    """Every figure shown by the funnel is rendered from the wrapper or the static KB: no `<number> <unit>` literal in the
    funnel's text modules (the module-level numbers rule is in test_no_constants.py)."""
    unit = re.compile(r"\d\s*(₽|руб|видео|клип|дн\b|дней|дня|язык|трек|канал|картин|сток|минут|мин\b|сутк)", re.IGNORECASE)
    hits = []
    for name in ("texts.py", "render.py"):
        tree = ast.parse((SRC / "funnel" / name).read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                    and unit.search(node.value)):
                hits.append((name, node.value[:60]))
    assert hits == []


def test_rendered_funnel_text_has_a_figure_only_where_it_comes_from_an_input():
    tariffs = parse_public_tariffs(funnel_tariffs())
    entry = tariffs.get(next(iter(tariffs.by_role)))
    text = funnel_texts.recommendation(entry, "n1_more", kb())
    assert "4 321" in text.replace(" ", " ") and "7 дн." in text
