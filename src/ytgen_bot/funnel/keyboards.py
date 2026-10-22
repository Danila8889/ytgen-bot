"""Funnel inline keyboards. Every button is a `f:<code>` callback (`callbacks.FUNNEL_CODES`) except the reminder opt-in
(`rm:yes|no`) and URL buttons (offer, the manager's t.me link built from SUPPORT_CONTACT)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import callbacks
from . import texts


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def _b(label: str, code: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callbacks.funnel(code))


def _row(*pairs: tuple[str, str]) -> list[InlineKeyboardButton]:
    return [_b(label, code) for label, code in pairs]


def manager_button(support: str, label: str = "Написать менеджеру") -> list[InlineKeyboardButton]:
    """The "write to the manager" URL button: `https://t.me/<handle>` built from SUPPORT_CONTACT (no button when
    SUPPORT_CONTACT is not a plain @handle; the text of the message still names it)."""
    url = texts.support_url(support)
    return [InlineKeyboardButton(text=label, url=url)] if url else []


def manager_markup(support: str) -> InlineKeyboardMarkup | None:
    row = manager_button(support)
    return _kb(row) if row else None


def manager_fallback(support: str) -> InlineKeyboardMarkup:
    """When the script cannot do its job (tariffs unavailable): the person can still reach a human."""
    return _kb(_row(("Связаться с человеком", "handoff_manual")), manager_button(support))


def reminders_choice() -> InlineKeyboardMarkup:
    return _kb([
        InlineKeyboardButton(text="Да, напоминать", callback_data=callbacks.reminders("yes")),
        InlineKeyboardButton(text="Нет, не нужно", callback_data=callbacks.reminders("no")),
    ])


def greeting() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Как это работает", "intro_how")),
        _row(("Подобрать тариф", "spin_start")),
        _row(("Посмотреть примеры видео", "examples")),
        _row(("Задать свой вопрос", "ask_free")),
        _row(("Написать человеку", "handoff_manual")),
    )


def how_it_works() -> InlineKeyboardMarkup:
    return _kb(_row(("Подобрать тариф", "spin_start"), ("Примеры видео", "examples")), _row(("Назад", "back_start")))


def examples() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Подобрать тариф", "spin_start")),
        _row(("Назад", "back_start"), ("Есть сомнения", "doubt_open")),
    )


def after_ai() -> InlineKeyboardMarkup:
    """After a free-text AI answer outside a SPIN question: the person chooses what is next."""
    return _kb(
        _row(("Подобрать тариф", "spin_start"), ("Задать ещё вопрос", "ask_free")),
        _row(("Написать человеку", "handoff_manual")),
    )


def after_ai_resume() -> InlineKeyboardMarkup:
    """After a free question in the middle of the script: back to the same step."""
    return _kb(_row(("Продолжить подбор", "resume")), _row(("Написать человеку", "handoff_manual")))


def _spin(rows: list[list[InlineKeyboardButton]], back: bool = True) -> InlineKeyboardMarkup:
    return _kb(*rows, _row(("Назад", "back")) if back else [])


def s1() -> InlineKeyboardMarkup:
    return _spin([
        _row(("Только планирую начать", "s1_plan")), _row(("Веду один канал", "s1_one")),
        _row(("Веду несколько каналов", "s1_multi")), _row(("Работаю с командой или студией", "s1_team")),
    ])


def s2() -> InlineKeyboardMarkup:
    return _spin([
        _row(("Пару в неделю", "s2_low")), _row(("Почти каждый день", "s2_mid")),
        _row(("Несколько в день", "s2_high")), _row(("Пока не знаю", "s2_unk")),
    ])


def s3() -> InlineKeyboardMarkup:
    return _spin([_row(("Пропустить", "s3_skip"))])


def p1() -> InlineKeyboardMarkup:
    return _spin([
        _row(("Сборка и монтаж роликов", "p1_edit")), _row(("Написание сценариев", "p1_script")),
        _row(("Озвучка", "p1_voice")), _row(("Много нейросетей и подписок", "p1_tools")),
        _row(("Не знаю, с чего начать", "p1_start")), _row(("Другое", "p1_other")),
    ])


def p1_other() -> InlineKeyboardMarkup:
    return _spin([])


def i1() -> InlineKeyboardMarkup:
    return _spin([
        _row(("Видео выходят реже, чем хочется", "i1_slow")),
        _row(("Не получается запускать новые каналы", "i1_noscale")),
        _row(("Уже устаю от рутины", "i1_tired")), _row(("Всё упирается в сроки и деньги", "i1_cost")),
        _row(("Пока не отражается", "i1_none")),
    ])


def n1() -> InlineKeyboardMarkup:
    return _spin([
        _row(("Выпускать больше видео за то же время", "n1_more")), _row(("Запустить ещё каналы", "n1_scale")),
        _row(("Быстро стартовать с нуля", "n1_start")),
        _row(("Освободить время для тем и стратегии", "n1_time")), _row(("Выйти на другие языки", "n1_lang")),
    ])


def recommendation(entry_name: str | None, rec_name: str, is_entry: bool) -> InlineKeyboardMarkup:
    pay_label = f"Оплатить {rec_name}"
    rows = [_row(("Показать подробнее", "show_details")), _row(("Сравнить тарифы", "compare"))]
    if not is_entry:  # when the recommendation IS the entry tariff, "try" and "pay" lead to the same place: one button
        rows.append(_row((f"Попробовать {entry_name}" if entry_name else "Попробовать недорогой тариф", "try_lite")))
    rows += [_row((pay_label, "pay_main")), _row(("Есть сомнения", "doubt_open")),
             _row(("Связаться с человеком", "handoff_manual"))]
    return _kb(*rows)


def details_a() -> InlineKeyboardMarkup:
    return _kb(_row(("Дальше", "details_next"), ("Примеры видео", "examples")))


def details_b() -> InlineKeyboardMarkup:
    return _kb(_row(("Дальше", "details_next"), ("Сравнить тарифы", "compare")))


def details_c() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Выбрать этот тариф", "pay_main")), _row(("Сравнить тарифы", "compare")),
        _row(("Есть сомнения", "doubt_open")), _row(("Связаться с человеком", "handoff_manual")),
    )


def compare() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Показать подробнее", "show_details")), _row(("Есть сомнения", "doubt_open")),
        _row(("Связаться с человеком", "handoff_manual")),
    )


def closing(entry_name: str | None, rec_name: str, is_entry: bool) -> InlineKeyboardMarkup:
    rows = []
    if not is_entry:
        rows.append(_row((f"Попробовать {entry_name}" if entry_name else "Попробовать недорогой тариф", "try_lite")))
    rows += [
        _row((f"Оплатить {rec_name}", "pay_main")), _row(("Посмотреть примеры", "examples")),
        _row(("Сравнить тарифы", "compare")), _row(("Связаться с человеком", "handoff_manual")),
    ]
    return _kb(*rows)


def objections() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Дорого", "obj_price")), _row(("Не верю, что ИИ сделает нормально", "obj_quality")),
        _row(("Сложно разобраться", "obj_hard")), _row(("Боюсь бана или демонетизации", "obj_ban")),
        _row(("У меня уже есть ChatGPT", "obj_gpt")), _row(("Нет времени", "obj_time")),
        _row(("Не уверен, что окупится", "obj_roi")), _row(("Что с возвратом", "obj_refund")),
        _row(("Хочу подумать", "obj_think")),
    )


def try_row(entry_name: str | None) -> list[InlineKeyboardButton]:
    return _row((f"Попробовать {entry_name}" if entry_name else "Попробовать недорогой тариф", "try_lite"))


def obj_price(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(try_row(entry_name), _row(("Сравнить тарифы", "compare")), _row(("Связаться с человеком", "handoff_manual")))


def obj_quality() -> InlineKeyboardMarkup:
    return _kb(_row(("Картинка и клипы", "q_visual")), _row(("Голос", "q_voice")), _row(("Шаблонность", "q_template")))


def after_quality(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(_row(("Примеры видео", "examples")), try_row(entry_name))


def obj_hard() -> InlineKeyboardMarkup:
    return _kb(_row(("Настройка", "h_setup"), ("YouTube", "h_yt")))


def after_hard(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(try_row(entry_name), _row(("Как это работает", "intro_how")), _row(("Написать человеку", "handoff_manual")))


def obj_ban(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(
        try_row(entry_name), _row(("Спросить человека", "handoff_manual")),
        [InlineKeyboardButton(text="Читать оферту", url=texts.OFFER)],
    )


def obj_gpt() -> InlineKeyboardMarkup:
    return _kb(_row(("Пишу сценарии", "g_script"), ("Собираю видео", "g_video")))


def obj_gpt_2() -> InlineKeyboardMarkup:
    return _kb(_row(("Нужно всё в одном", "g_all"), ("Хватает текста", "g_text")))


def obj_time(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(try_row(entry_name), _row(("Как это работает", "intro_how")), _row(("Написать человеку", "handoff_manual")))


def obj_roi(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(try_row(entry_name), _row(("Разобрать мою нишу с человеком", "handoff_manual")))


def obj_refund(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="Читать оферту", url=texts.OFFER)], try_row(entry_name),
        _row(("Спросить человека", "handoff_manual")),
    )


def obj_think() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Посмотреть примеры видео", "examples")), _row(("Сохранить подбор и напомнить позже", "remind_ok")),
        _row(("Задать вопрос человеку", "handoff_manual")), _row(("Пока всё", "end_polite")),
    )


def repeat_objection() -> InlineKeyboardMarkup:
    return _kb(_row(("Поговорить с человеком", "handoff_manual")))


def pay_howto() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Перейти к оплате", "go_pay")), _row(("Нужна помощь с оплатой", "handoff_manual")),
        _row(("Назад", "back_close")),
    )


def pay_check() -> InlineKeyboardMarkup:
    return _kb(_row(("Да, всё работает", "pay_done")), _row(("Нужна помощь", "handoff_manual")),
               _row(("Пока нет", "pay_not_yet")))


def pay_recheck() -> InlineKeyboardMarkup:
    return _kb(_row(("Проверить ещё раз", "pay_done")), _row(("Нужна помощь", "handoff_manual")))


def handoff_done(support: str) -> InlineKeyboardMarkup | None:
    row = manager_button(support, f"Написать {support}")
    return _kb(row) if row else None


def discount() -> InlineKeyboardMarkup:
    return _kb(_row(("Выбрать тариф и оплатить", "pay_main")), _row(("Написать человеку", "handoff_manual")))


def f1() -> InlineKeyboardMarkup:
    return _kb(_row(("Продолжить подбор", "resume"), ("Не сейчас", "end_polite")), _row(("Больше не напоминать", "unsub")))


def f2() -> InlineKeyboardMarkup:
    return _kb(
        _row(("Задать вопрос", "ask_free"), ("Подобрать тариф", "spin_start")), _row(("Больше не напоминать", "unsub")),
    )


def f3(entry_name: str | None) -> InlineKeyboardMarkup:
    return _kb(
        try_row(entry_name), _row(("Связаться с человеком", "handoff_manual")), _row(("Больше не напоминать", "unsub")),
    )


def end_polite() -> InlineKeyboardMarkup:
    return _kb(_row(("Подобрать тариф", "spin_start")))
