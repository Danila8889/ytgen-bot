"""Output validator (PDF section 10, defense 1): runs on every AI reply BEFORE it is sent.

  * every number next to a unit (money, videos, clips, days, languages, tracks, ...) must appear in the knowledge-base text
    rendered for that same call (static facts + live tariff block);
  * the forbidden-phrase list (guarantees, "никогда не", a hundred percent, no-risk / demonetisation / safe-for-YouTube claims, claims of
    publishing to YouTube, scarcity, computer vision / frame checking, auxiliary scenes, dollar prices, percentages) blocks it.

A blocked reply is replaced by the script ("Тут лучше уточнить у менеджера" + the hand-off button); the caller logs a
WARNING and the ai_log outcome `validator_blocked`. Limits: a number spelled out in words is not checked.
"""
from __future__ import annotations

import dataclasses
import re

_NBSP = "\u00a0\u202f"
_UNIT = (
    r"(?:₽|руб\w*|видео\w*|ролик\w*|клип\w*|дн\w*|день|дней|дня|язык\w*|трек\w*|картин\w*|сток\w*|канал\w*|"
    r"сцен\w*|ссыл\w*|мин\w*|час\w*|недел\w*|месяц\w*|сутк\w*|секунд\w*|%|\$|процент\w*|лет|год\w*)"
)
_NUMBER_BEFORE_UNIT = re.compile(rf"(\d+(?:[.,]\d+)?)\s*(?:[^\W\d_]+\s+){{0,2}}?{_UNIT}", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_THOUSANDS = re.compile(rf"(?<=\d)[ {_NBSP}](?=\d{{3}}(?!\d))")

FORBIDDEN: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("guarantee", r"гарант|guarantee"),
        ("never", r"никогда\s+не|never\s+(?:be|get|lose)"),
        ("hundred_percent", r"100\s*%|сто\s+процент"),
        ("no_risk", r"без\s+риска|risk[- ]free|no\s+risk"),
        ("demonetisation", r"не\s+демонетиз|демонетизац\w+\s+не\s+(?:будет|грозит)"),
        ("protected", r"защищен|защищён|защит\w+\s+от\s+(?:бана|блокиров|демонетиз)"),
        ("safe_for_youtube", r"безопасн\w+\s+для\s+(?:youtube|ютуб)"),
        (
            "publishes",
            (
                r"(?<!не\s)публикуе\w*[^.!?\n]{0,40}(?:youtube|ютуб)|"
                r"(?<!не\s)(?:загружае\w*|заливае\w*|выкладывае\w*)[^.!?\n]{0,40}(?:youtube|ютуб)|"
                r"автозалив|автопубликац"
            ),
        ),
        ("scarcity", r"осталось\s+(?:\d+\s+)?мест|последни\w+\s+мест|акци\w+\s+до\s|только\s+сегодня|успей"),
        (
            "computer_vision",
            (
                r"компьютерн\w+\s+зрени|computer\s+vision|"
                r"(?<!не\s)(?:проверя\w+|оценива\w+|анализиру\w+)\s+(?:все\s+)?кадр|"
                r"проверк\w+\s+кадр|автоматическ\w+\s+перегенер|перегенер\w+\s+автоматическ"
            ),
        ),
        ("auxiliary_scenes", r"вспомогательн\w+\s+сцен"),
        ("dollars", r"\$|доллар|\busd\b"),
        ("percent", r"\d\s*%|процент"),
    )
)


@dataclasses.dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def _normalise_numbers(text: str) -> str:
    return _THOUSANDS.sub("", text).replace(",", ".")


def numbers_in(text: str) -> set[str]:
    return {m.group(0).rstrip(".") for m in _NUMBER.finditer(_normalise_numbers(text))}


def validate(reply: str, kb_text: str) -> Verdict:
    reasons: list[str] = []
    folded = reply.lower().replace("ё", "е")
    for name, pattern in FORBIDDEN:
        if pattern.search(folded):
            reasons.append(f"forbidden:{name}")
    known = numbers_in(kb_text)
    for match in _NUMBER_BEFORE_UNIT.finditer(_normalise_numbers(reply)):
        number = match.group(1).rstrip(".")
        if number not in known:
            reasons.append(f"number_not_in_kb:{number}")
    return Verdict(ok=not reasons, reasons=tuple(reasons))
