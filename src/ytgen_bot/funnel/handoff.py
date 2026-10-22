"""The card sent to the manager chat on a hand-off (PDF M5.4): who, where from, the SPIN answers, the recommended tariff,
the objections shown, the reason and priority, and the person's last five free-text messages."""
from __future__ import annotations

from ..formatting import esc
from .model import CARD_CLIP, Lead


def _c(value: str | None) -> str:
    return esc((value or "—")[:CARD_CLIP])

REASON_LABELS = {
    "manual_request": "человек просит связаться (кнопка)",
    "payment_method": "другой способ оплаты (оплата)",
    "payment_help": "нужна помощь с оплатой (оплата)",
    "discount": "вопрос про скидку или акцию",
    "refund_dispute": "возврат или спор по оплате",
    "complaint": "жалоба или резкий тон",
    "repeat_objection": "то же возражение во второй раз",
    "ai_no_answer": "AI не нашёл ответ в базе знаний",
    "ai_handoff": "AI передал вопрос человеку",
    "ai_unavailable": "AI недоступен",
    "tariff_unavailable": "не удалось подобрать тариф автоматически",
}
PRIORITY_LABELS = {"normal": "обычный", "high": "ВЫСОКИЙ"}


def card(lead: Lead, *, reason: str, priority: str, objections: list[str], messages: list[str]) -> str:
    name = esc(lead.first_name or "—")
    handle = f"@{esc(lead.username)}" if lead.username else "без username"
    rec = f"{lead.rec_role} (tariff id {lead.rec_tariff_id})" if lead.rec_role else "ещё не подобран"
    spin = [
        f"stage: {esc(lead.stage or '—')}",
        f"volume: {esc(lead.volume or '—')}",
        f"niche_text: {_c(lead.niche_text)}",
        f"pain: {esc(lead.pain or '—')}" + (f" / {_c(lead.pain_text)}" if lead.pain_text else ""),
        f"impact: {esc(lead.impact or '—')}",
        f"goal: {esc(lead.goal or '—')}",
    ]
    lines = [
        f"<b>Передача менеджеру</b> · приоритет: <b>{PRIORITY_LABELS.get(priority, esc(priority))}</b>",
        f"Причина: {esc(REASON_LABELS.get(reason, reason))}",
        f"Клиент: {name}, {handle}, tg_id <code>{lead.tg_id}</code>",
        f"Источник: {esc(lead.source or 'не указан')}",
        "",
        "<b>Ответы SPIN</b>",
        *spin,
        "",
        f"Рекомендованный тариф: {esc(rec)}",
        f"Показанные возражения: {esc(', '.join(objections)) if objections else '—'}",
        "",
        "<b>Последние сообщения клиента</b>",
    ]
    if messages:
        lines += [f"{i}. {_c(m)}" for i, m in enumerate(messages, 1)]
    else:
        lines.append("—")
    return "\n".join(lines)
