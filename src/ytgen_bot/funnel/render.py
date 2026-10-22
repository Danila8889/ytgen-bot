"""Tariff facts rendered as text. Every figure comes from the wrapper's tariff row; the only words are structural."""
from __future__ import annotations

from ..formatting import esc, rub
from .tariffs import FunnelTariff, TariffSet

DAY_HOURS = 24  # a day is 24 hours: structural, not a business number


def window_phrase(hours: int) -> str:
    """The period a per-window figure applies to, from `capacity.window_hours` (never from a constant)."""
    if hours == DAY_HOURS:
        return "в сутки"
    if hours % DAY_HOURS == 0:
        return f"за {hours // DAY_HOURS} дн."
    return f"за {hours} ч"


def period(tariff: FunnelTariff) -> str:
    return f"{tariff.period_days} дн."


def price_period(tariff: FunnelTariff) -> str:
    return f"{rub(tariff.price_rub)} на {period(tariff)}"


def ai_videos(tariff: FunnelTariff) -> str:
    cap = tariff.capacity
    return f"до {cap.ai_video_videos_per_window} на ИИ-клипах {window_phrase(cap.window_hours)}"


def _other_window(tariff: FunnelTariff) -> int:
    """Image/stock videos are per UTC day on 24 h plans and per rolling window on short plans."""
    cap = tariff.capacity
    return DAY_HOURS if cap.image_stock_videos_per_day is not None else cap.window_hours


def other_videos(tariff: FunnelTariff) -> str:
    return f"до {tariff.image_stock_videos} на картинках и стоках {window_phrase(_other_window(tariff))}"


def limits_phrase(tariff: FunnelTariff) -> str:
    """One sentence about how many videos the plan allows (two parts, as the wrapper reports them)."""
    return f"{esc(tariff.name)} позволяет видео: {ai_videos(tariff)} и {other_videos(tariff)}."


def limits_phrase_short(tariff: FunnelTariff) -> str:
    """For objection answers: `до N на ИИ-клипах ... и до M на картинках и стоках ...` without the tariff name."""
    return f"видео: {ai_videos(tariff)} и {other_videos(tariff)}"


def limits_lines(tariff: FunnelTariff) -> list[str]:
    cap = tariff.capacity
    lines = [
        f"Видео: {ai_videos(tariff)}; {other_videos(tariff)}",
        f"до {cap.ai_video_scenes_per_day} ИИ-клипов в сутки",
        f"до {cap.images_per_day} картинок и до {cap.stock_per_day} стоковых видео в сутки",
    ]
    if tariff.max_ai_scenes_per_video is not None:
        lines.append(f"до {tariff.max_ai_scenes_per_video} ИИ-клипов в одном видео")
    return lines


def kb_tariff_block(tariffs: TariffSet) -> str:
    """The live tariff block of `{{KB}}` (plain text for the LLM; the validator checks numbers against it)."""
    rows = ["ТАРИФЫ (актуальные данные сервиса, единственный источник цен и лимитов):"]
    for _, t in tariffs.ordered:
        cap = t.capacity
        parts = [
            f"{t.name}: {rub(t.price_rub)} за {t.period_days} дн.",
            f"видео на ИИ-клипах: до {cap.ai_video_videos_per_window} {window_phrase(cap.window_hours)}",
            "видео на картинках и стоках: до "
            + f"{t.image_stock_videos} {window_phrase(_other_window(t))}",
            f"до {cap.ai_video_scenes_per_day} ИИ-клипов в сутки",
            f"до {cap.images_per_day} картинок в сутки",
            f"до {cap.stock_per_day} стоковых видео в сутки",
        ]
        if t.max_ai_scenes_per_video is not None:
            parts.append(f"до {t.max_ai_scenes_per_video} ИИ-клипов в одном видео")
        rows.append("- " + "; ".join(parts))
    return "\n".join(rows)
