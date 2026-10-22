"""All user-facing text (Russian, short, plain). HTML parse mode: every dynamic value goes through `esc`.

No price, limit or percentage is written here: every figure shown comes from a wrapper response and is
passed in as an argument (tests/test_no_constants.py enforces it).
"""
from __future__ import annotations

import datetime as dt

from . import schemas
from .formatting import esc, rub, wait, when

SITE_URL = "https://ytgenerator.ru"
OFFER_URL = "https://ytgenerator.ru/legal/oferta"
OFFER_REFERRAL_URL = "https://ytgenerator.ru/legal/oferta#section-10"
PRIVACY_URL = "https://ytgenerator.ru/legal/privacy"
PRIVACY_BOT_URL = f"{PRIVACY_URL}#section-11"

PRODUCT_NOTE = "Сам сервис в Telegram не используется: он работает на сайте ytgenerator.ru."

GENERIC_ERROR = "Что-то пошло не так. Попробуйте ещё раз через минуту."
SERVICE_UNAVAILABLE = "Сервис временно недоступен. Повторите попытку через минуту."
TOO_FAST = "Слишком часто. Подождите {wait} и повторите."
TOO_LONG = "Сообщение слишком длинное."
TEXT_ONLY = "Отправьте ответ обычным текстом."
STALE_BUTTON = "Эта кнопка устарела. Откройте меню: /start"
ALREADY_PROCESSING = "Уже обрабатываю, подождите пару секунд."
CONSENT_FIRST = "Сначала подтвердите согласие."
UNKNOWN_MESSAGE = "Не понял сообщение. Откройте меню: /start или справку: /help."
GROUP_IGNORED = "Бот работает только в личных сообщениях."


def too_fast(seconds: int) -> str:
    return TOO_FAST.format(wait=wait(seconds))


def _clause(number: str, label: str) -> str:
    return f'<a href="{PRIVACY_URL}#clause-11-{number}">{label}</a>'


def consent_request() -> str:
    return (
        "<b>Перед началом</b>\n"
        "Этот бот нужен для оплаты тарифа YouTubeGen, реферальной программы и подбора тарифа под вашу задачу "
        "(короткие вопросы, ответы на свободные вопросы, передача менеджеру). "
        f"{PRODUCT_NOTE}\n\n"
        "Пожалуйста, ознакомьтесь:\n"
        f'• <a href="{OFFER_REFERRAL_URL}">условия реферальной программы</a> (раздел 10 оферты);\n'
        f'• <a href="{OFFER_URL}">оферта</a>;\n'
        f'• <a href="{PRIVACY_BOT_URL}">обработка данных в боте</a> '
        "(политика конфиденциальности, раздел 11), в частности:\n"
        f"– {_clause('9', 'кто вы и откуда пришли')} (п. 11.9);\n"
        f"– {_clause('10', 'ваши ответы и свободный текст')} (п. 11.10);\n"
        f"– {_clause('11', 'возражения, подобранный тариф, время действий')} (п. 11.11);\n"
        f"– {_clause('12', 'цели обработки')} (п. 11.12);\n"
        f"– {_clause('13', 'передача менеджеру')} (п. 11.13);\n"
        f"– {_clause('14', 'напоминания')} (п. 11.14);\n"
        f"– {_clause('15', 'AI-ассистент (kie.ai) и журнал его ответов')} (п. 11.15);\n"
        f"– {_clause('16', 'удаление по команде /stop и срок хранения')} (п. 11.16).\n\n"
        "Нажимая «Согласен», вы подтверждаете, что ознакомились с этими документами. "
        "Отказаться можно в любой момент командой /stop."
    )


def consent_given() -> str:
    return f"Согласие получено. {PRODUCT_NOTE}"


def menu_prompt() -> str:
    return "Выберите действие:"


def help_text(support_contact: str) -> str:
    return (
        "<b>YouTubeGen Bot</b>\n"
        "Бот нужен только для оплаты тарифа и реферальной программы. "
        f"{PRODUCT_NOTE}\n\n"
        "/tariffs — тарифы и оплата\n"
        "/referral — реферальная программа\n"
        "/status — аккаунт и тариф\n"
        "/stop — отказаться от сообщений бота и удалить ваши ответы подбора\n"
        "/help — эта справка\n\n"
        f"Поддержка: {esc(support_contact)}\n"
        f'<a href="{OFFER_URL}">Оферта</a> · '
        f'<a href="{PRIVACY_BOT_URL}">Политика конфиденциальности</a>'
    )


def stopped() -> str:
    return "Готово: вы отказались от сообщений бота. Чтобы вернуться, отправьте /start."


def stop_purge_failed(support_contact: str) -> str:
    return (
        "Но удалить ваши ответы из подбора тарифа сейчас не удалось. Напишите в поддержку, мы удалим их вручную: "
        f"{esc(support_contact)}."
    )


def referral_outcome(outcome: str) -> str | None:
    """What the wrapper decided about a referral code; None = nothing worth saying."""
    return {
        "pending_stored": "Реферальная ссылка принята: она будет применена после привязки аккаунта.",
        "attributed": "Реферальная ссылка принята.",
        "already_attributed": "Ваш аккаунт уже связан с приглашением, повторно применить ссылку нельзя.",
        "already_pending": "Реферальная ссылка уже сохранена ранее.",
        "rejected_self_referral": "Свою собственную реферальную ссылку использовать нельзя.",
        "rejected_ineligible": "Эта реферальная ссылка к вашему аккаунту не применяется.",
        "rejected_unknown_code": "Реферальная ссылка недействительна.",
    }.get(outcome)


# --- account link ---------------------------------------------------------------------------------


def link_need_account() -> str:
    return "Для оплаты нужно привязать аккаунт. Это займёт минуту."


def link_ask_email() -> str:
    return (
        "Введите email аккаунта на сайте. Мы отправим на него 6-значный код. "
        "Если аккаунта с таким email нет, он будет создан после подтверждения.\n"
        "Отмена: /start"
    )


def link_bad_email() -> str:
    return "Это не похоже на email. Проверьте адрес и отправьте ещё раз."


def link_code_sent(email_masked: str, expires_in_s: int) -> str:
    minutes = max(1, round(expires_in_s / 60))
    return (
        f"Код отправлен на {esc(email_masked)}. Он действует {minutes} мин.\n"
        "Введите 6 цифр из письма."
    )


def link_bad_code_format() -> str:
    return "Код состоит из 6 цифр. Введите его ещё раз."


def link_done(result: schemas.LinkVerifyResponse) -> str:
    if result.already_linked:
        return "Этот Telegram уже привязан к вашему аккаунту."
    if result.tenant_created:
        return "Готово: аккаунт создан и привязан к этому Telegram."
    return "Готово: аккаунт привязан к этому Telegram."


# --- status ---------------------------------------------------------------------------------------


def status_unlinked() -> str:
    return "Аккаунт не привязан к этому Telegram."


def status_linked(tenant: schemas.MeTenant) -> str:
    lines = [f"Аккаунт: {esc(tenant.email_masked)}"]
    if tenant.tariff_name and tenant.paid_access_active and tenant.tariff_expires_at:
        lines.append(f"Тариф: {esc(tenant.tariff_name)}, действует до {when(tenant.tariff_expires_at)}")
    elif tenant.tariff_name and tenant.tariff_expires_at:
        lines.append(f"Тариф: {esc(tenant.tariff_name)}, срок действия истёк {when(tenant.tariff_expires_at)}")
    else:
        lines.append("Активного тарифа нет.")
    return "\n".join(lines)


def back_from_payment(me: schemas.MeResponse) -> str:
    """`/start paid` when the wrapper knows no payment of this user: show the account state instead."""
    base = "Платёж, оформленный через бота, не найден. Вот текущее состояние аккаунта.\n\n"
    if me.linked and me.tenant is not None:
        return base + status_linked(me.tenant)
    return base + status_unlinked()


# --- tariffs --------------------------------------------------------------------------------------


def _window(hours: int | None) -> str:
    if hours is None:
        return "за период"
    if hours == 24:
        return "в сутки"
    if hours % 24 == 0:
        return f"за {hours // 24} дн."
    return f"за {hours} ч"


def tariff_block(tariff: schemas.Tariff) -> str:
    lines = [f"<b>{esc(tariff.name)}</b> — {rub(tariff.price_rub)} за {tariff.period_days} дн."]
    cap = tariff.capacity
    if cap is not None:
        if cap.ai_video_videos_per_window is not None:
            lines.append(
                f"Видео с ИИ-сценами: до {cap.ai_video_videos_per_window} {_window(cap.window_hours)}"
            )
        if cap.image_stock_videos_per_day is not None:
            lines.append(f"Видео из изображений и стока: до {cap.image_stock_videos_per_day} в сутки")
        elif cap.image_stock_videos_per_window is not None:
            lines.append(
                f"Видео из изображений и стока: до {cap.image_stock_videos_per_window} "
                f"{_window(cap.window_hours)}"
            )
    if tariff.max_ai_scenes_per_video is not None:
        lines.append(f"ИИ-сцен в одном видео: до {tariff.max_ai_scenes_per_video}")
    return "\n".join(lines)


def tariffs_list(tariffs: list[schemas.Tariff]) -> str:
    if not tariffs:
        return "Сейчас нет доступных тарифов. Загляните позже."
    return "<b>Тарифы</b>\n\n" + "\n\n".join(tariff_block(t) for t in tariffs) + "\n\nВыберите тариф для оплаты:"


def tariff_chosen(tariff: schemas.Tariff) -> str:
    return (
        f"{tariff_block(tariff)}\n\n"
        "Есть промокод? Введите его или продолжите без него."
    )


def active_other_tariff(expires_at: dt.datetime | None) -> str:
    until = f" до {when(expires_at)}" if expires_at is not None else ""
    return (
        f"Сейчас у вас действует другой тариф{until}. "
        "Оформить другой тариф через бота можно после окончания срока действия текущего."
    )


def promo_ask() -> str:
    return "Введите промокод одним сообщением. Отмена: /start"


def promo_bad_format() -> str:
    return "Промокод не подошёл по формату (латинские буквы, цифры, «-» и «_»). Введите ещё раз."


def quote_text(quote: schemas.QuoteResponse) -> str:
    lines = [f"Тариф: <b>{esc(quote.plan_name)}</b>, {quote.period_days} дн."]
    if quote.promo is not None:
        lines.append(f"Цена по прайсу: {rub(quote.promo.list_price_rub)}")
        lines.append(
            f"Промокод {esc(quote.promo.code)}: −{rub(quote.promo.discount_rub)} ({quote.promo.percent}%)"
        )
    lines.append(f"К оплате: <b>{rub(quote.payable_rub)}</b>")
    return "\n".join(lines)


def checkout_created(amount_rub: int | None, expected_rub: int) -> str:
    text = "Платёж создан. Нажмите «Оплатить», чтобы перейти к оплате."
    if amount_rub is not None and amount_rub != expected_rub:
        text += (
            f"\n\nВнимание: сумма к оплате {rub(amount_rub)}, а не {rub(expected_rub)}, "
            "как было показано ранее. Проверьте её на странице оплаты."
        )
    elif amount_rub is not None:
        text += f"\nСумма: {rub(amount_rub)}"
    text += "\nПосле оплаты вернитесь в бот: статус платежа обновится здесь."
    return text


def checkout_reused(amount_rub: int | None) -> str:
    amount = f" Сумма: {rub(amount_rub)}." if amount_rub is not None else ""
    return (
        "Ранее созданная ссылка на оплату всё ещё действует, используйте её: "
        f"новый платёж не создаётся.{amount}"
    )


def processing() -> str:
    return "Платёж обрабатывается. Я напишу, когда будет результат; проверить можно и кнопкой ниже."


def payment_still_pending() -> str:
    return "Платёж ещё обрабатывается."


def payment_pending_timeout() -> str:
    return (
        "Платёж пока не подтверждён. Если вы уже оплатили, это может занять время. "
        "Проверьте позже кнопкой «Проверить оплату»."
    )


def payment_credit_delayed() -> str:
    return (
        "Оплата получена, но тариф ещё не применился. "
        "Проверьте через минуту кнопкой «Проверить оплату»; если не появится — напишите в поддержку."
    )


def payment_success(me: schemas.MeResponse | None) -> str:
    text = "Оплата прошла."
    tenant = me.tenant if me is not None else None
    if tenant is not None and tenant.tariff_name and tenant.tariff_expires_at:
        return (
            f"{text} Тариф: {esc(tenant.tariff_name)}, действует до {when(tenant.tariff_expires_at)}."
        )
    return f"{text} Не удалось получить данные тарифа: посмотрите их командой /status."


_CANCEL_REASONS = {
    "expired_on_confirmation": "Время на оплату истекло.",
    "insufficient_funds": "Недостаточно средств.",
    "3d_secure_failed": "Не пройдена проверка 3-D Secure.",
    "card_expired": "Срок действия карты истёк.",
    "canceled_by_merchant": "Платёж отменён.",
    "permission_revoked": "Платёж отменён: разрешение на списание отозвано.",
}


def payment_canceled(reason: str | None) -> str:
    detail = _CANCEL_REASONS.get(reason or "", "")
    return (
        "Платёж не прошёл, деньги не списаны." + (f"\n{detail}" if detail else "")
        + "\nМожно попробовать ещё раз: /tariffs"
    )


# --- referral -------------------------------------------------------------------------------------


def referral_screen(info: schemas.ReferralInfo, support_contact: str) -> str:
    lines = ["<b>Реферальная программа</b>"]
    if info.deep_link:
        lines.append(f"Ваша ссылка: {esc(info.deep_link)}")
    else:
        lines.append("Реферальная ссылка временно недоступна. Попробуйте позже.")
    c, b = info.counts, info.balance
    lines += [
        "",
        f"Переходов по ссылке: {c.opens}",
        f"Привязано пользователей: {c.attributed}",
        f"Оплатили: {c.paid}",
        "",
        f"Ожидает (удержание): {rub(b.pending_rub)}",
        f"Доступно к выводу: {rub(b.available_rub)}",
        f"В заявке на вывод: {rub(b.requested_rub)}",
        f"Выплачено: {rub(b.paid_out_rub)}",
    ]
    if b.owed_rub > 0:
        lines.append(f"К удержанию из будущих начислений: {rub(b.owed_rub)}")
    lines += [
        "",
        (
            f"Вознаграждение: {info.reward_percent}%. Удержание: {info.hold_days} дн. "
            f"Минимальный вывод: {rub(info.min_withdrawal_rub)}."
        ),
        f'Полные условия: <a href="{OFFER_REFERRAL_URL}">раздел 10 оферты</a>.',
    ]
    if info.open_withdrawal is not None:
        ow = info.open_withdrawal
        lines += [
            "",
            (
                f"Заявка на вывод №{ow.id}: {rub(ow.amount_rub)} от {when(ow.created_at)}. "
                f"Вопросы по заявке: {esc(support_contact)}"
            ),
        ]
    return "\n".join(lines)


def withdraw_ask(info: schemas.ReferralInfo) -> str:
    return (
        f"Введите сумму вывода в рублях, целым числом: от {rub(info.min_withdrawal_rub)} "
        f"до {rub(info.balance.available_rub)}.\n"
        "Реквизиты (карту, телефон) писать сюда не нужно и не следует. Отмена: /start"
    )


def withdraw_bad_amount() -> str:
    return "Нужно целое число рублей, только цифры. Введите сумму ещё раз."


def withdraw_confirm(amount_rub: int) -> str:
    return f"Запросить вывод {rub(amount_rub)}?"


def withdraw_done(amount_rub: int, request_id: int, support_contact: str) -> str:
    return (
        f"Заявка №{request_id} на вывод {rub(amount_rub)} создана. "
        "Оператор свяжется с вами в Telegram. "
        f"Вопросы: {esc(support_contact)}.\n"
        "Реквизиты в этот чат не отправляйте."
    )


def withdraw_unavailable() -> str:
    return "Сейчас нечего выводить: нет доступной суммы."

