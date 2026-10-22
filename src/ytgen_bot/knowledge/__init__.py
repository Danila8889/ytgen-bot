"""The static knowledge base (`knowledge_base.toml`): non-tariff product facts, each with its source.

Tariff money, limits, periods and names are NOT here: they are rendered live from the wrapper (`funnel.tariffs`).
"""
from __future__ import annotations

import dataclasses
import tomllib
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).with_name("knowledge_base.toml")


@dataclasses.dataclass(frozen=True)
class Fact:
    id: str
    text: str
    source: str


@dataclasses.dataclass(frozen=True)
class Example:
    title: str
    url: str


class KnowledgeBase:
    def __init__(self, facts: list[Fact], examples: list[Example], support_contact: str) -> None:
        self._facts = {f.id: f for f in facts}
        if len(self._facts) != len(facts):
            raise ValueError("duplicate fact id in the knowledge base")
        self._support = support_contact
        self.examples = examples

    def _render(self, text: str) -> str:
        return text.replace("{SUPPORT_CONTACT}", self._support)

    def fact(self, fact_id: str) -> str:
        return self._render(self._facts[fact_id].text)

    @property
    def facts(self) -> list[Fact]:
        return [dataclasses.replace(f, text=self._render(f.text)) for f in self._facts.values()]

    def facts_block(self) -> str:
        """The static part of `{{KB}}`: one bullet per fact (sources stay in the file, they are not prompt text)."""
        return "\n".join(f"- {f.text}" for f in self.facts)


@lru_cache(maxsize=1)
def _load_raw() -> tuple[tuple[Fact, ...], tuple[Example, ...]]:
    data = tomllib.loads(PATH.read_text(encoding="utf-8"))
    facts = tuple(Fact(f["id"], f["text"], f["source"]) for f in data["fact"])
    examples = tuple(Example(e["title"], e["url"]) for e in data["examples"])
    return facts, examples


def load(support_contact: str) -> KnowledgeBase:
    facts, examples = _load_raw()
    return KnowledgeBase(list(facts), list(examples), support_contact)
