"""Deterministic hand-off triggers on free text (PDF section 8 trigger table). They run BEFORE the AI, so they work when the
AI is down and the script - not the model - decides who talks to a human.

  payment_method  other ways to pay (cards of other systems, crypto, invoices, legal entities)  -> hand-off
  discount        discounts, promotions, promo campaigns                                          -> M5.5 (+ optional hand-off)
  refund_dispute  refund details, a payment dispute, double charge                                -> hand-off, HIGH priority
  complaint       negativity, a complaint, a sharp tone                                           -> hand-off, HIGH priority
"""
from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "refund_dispute",
            (
                r"верни\w*\s+(?:мне\s+)?деньги|вернуть\s+деньги|двойн\w+\s+списани|"
                r"списал\w*\s+(?:дважды|два\s+раза)|оспорить|чарджбэк|chargeback|диспут|претензи"
            ),
        ),
        ("complaint", r"жалоб|обман|мошенни|развод|лохотрон|ужасн|отврат|бесит|безобраз|кидал|кидают"),
        (
            "payment_method",
            (
                r"visa|виза\b|usdt|крипт|bitcoin|биткоин|paypal|пэйпал|apple\s*pay|google\s*pay|"
                r"по\s+счет\w*|выставит\w*\s+счет|инвойс|invoice|безнал|юр\.?\s*лиц|для\s+(?:ип|ооо)\b|"
                r"перевод\w*\s+на\s+карт|оплат\w+\s+(?:иностранн|зарубежн)"
            ),
        ),
        (
            "discount",
            r"скидк|акци[яиюе]\b|промокод|промо-?код|купон|подешевле|дешевле|распродаж|кэшбэк|cashback|бонус",
        ),
    )
)

HIGH_PRIORITY = frozenset({"refund_dispute", "complaint"})


def classify(text: str) -> str | None:
    folded = text.lower().replace("ё", "е")
    for name, pattern in _PATTERNS:
        if pattern.search(folded):
            return name
    return None
