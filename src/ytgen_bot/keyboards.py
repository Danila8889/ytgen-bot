"""Inline keyboards."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import callbacks, schemas
from .formatting import rub


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def consent(ref_code: str | None = None) -> InlineKeyboardMarkup:
    return _kb([_btn("Согласен", callbacks.consent(ref_code))])


def main_menu() -> InlineKeyboardMarkup:
    return _kb(
        [_btn("Тарифы и оплата", callbacks.menu("tariffs"))],
        [_btn("Реферальная программа", callbacks.menu("referral"))],
        [_btn("Мой статус", callbacks.menu("status")), _btn("Помощь", callbacks.menu("help"))],
    )


def link_prompt(tariff_id: int = 0) -> InlineKeyboardMarkup:
    return _kb([_btn("Привязать аккаунт", callbacks.link(tariff_id))])


def link_retry(tariff_id: int = 0) -> InlineKeyboardMarkup:
    return _kb([_btn("Запросить код заново", callbacks.link(tariff_id))])


def tariffs(items: list[schemas.Tariff]) -> InlineKeyboardMarkup:
    return _kb(*[[_btn(f"{t.name} — {rub(t.price_rub)}", callbacks.tariff(t.id))] for t in items])


def tariff_actions(tariff_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("Ввести промокод", callbacks.promo(tariff_id))],
        [_btn("Продолжить без промокода", callbacks.quote(tariff_id))],
    )


def pay_confirm(tariff_id: int, payable_rub: int, promo_code: str | None) -> InlineKeyboardMarkup:
    return _kb([_btn(f"Перейти к оплате {rub(payable_rub)}", callbacks.pay(tariff_id, payable_rub, promo_code))])


def payment(payment_url: str, payment_id: str, amount_rub: int | None) -> InlineKeyboardMarkup:
    label = f"Оплатить {rub(amount_rub)}" if amount_rub is not None else "Оплатить"
    return _kb(
        [InlineKeyboardButton(text=label, url=payment_url)],
        [_btn("Проверить оплату", callbacks.check(payment_id))],
    )


def check_payment(payment_id: str) -> InlineKeyboardMarkup:
    return _kb([_btn("Проверить оплату", callbacks.check(payment_id))])


def status_actions(linked: bool) -> InlineKeyboardMarkup:
    if not linked:
        return link_prompt()
    return _kb([_btn("Тарифы и оплата", callbacks.menu("tariffs"))])


def referral_actions(can_withdraw: bool) -> InlineKeyboardMarkup | None:
    if not can_withdraw:
        return None
    return _kb([_btn("Запросить вывод", callbacks.withdraw("start"))])


def withdraw_confirm(amount_rub: int) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("Подтвердить", callbacks.withdraw_ok(amount_rub)), _btn("Отмена", callbacks.withdraw("cancel"))]
    )
