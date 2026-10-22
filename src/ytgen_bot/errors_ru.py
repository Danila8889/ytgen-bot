"""Wrapper error code -> Russian message, per call context. The documented codes live here and only here.

Contexts name the call that failed (`link_request`, `link_verify`, `quote`, `checkout`, `payment`,
`withdrawal`, `me`, `generic`). A code with no entry in its context falls back to the common table and
then to a generic retry message; that fallback is logged at ERROR by `describe` so a new, unmapped wrapper
error never disappears silently.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from . import texts
from .formatting import esc, rub, wait
from .wrapper import WrapperContractError, WrapperError, WrapperUnavailable

log = logging.getLogger(__name__)

# Operator-side problems (key, config): not the user's to fix, but never silent.
# return_url_not_allowed = the bot's username (getMe) differs from the wrapper's BOT_DEEP_LINK_BASE.
MISCONFIG = {
    "invalid_bot_service_key",
    "bot_api_disabled",
    "bot_return_url_not_configured",
    "return_url_not_allowed",
    "invalid_telegram_user_id",
}


@dataclasses.dataclass(frozen=True)
class ErrorView:
    text: str
    # Follow-up: "link" (offer the link flow), "restart" (ask for a new code), "retry", "none".
    next: str = "retry"


def _rate_limited(err: WrapperError) -> str:
    seconds = err.retry_after_s
    if seconds is None:
        return "Слишком много запросов. Повторите чуть позже."
    return f"Слишком много запросов. Повторите через {wait(seconds)}."


def _support(support: str) -> str:
    return f"Обратитесь в поддержку: {esc(support)}."


def describe(exc: Exception, ctx: str, support: str) -> ErrorView:
    """The message for any failure of a wrapper call; never raises, never returns an empty text."""
    if isinstance(exc, WrapperUnavailable):
        return ErrorView(texts.SERVICE_UNAVAILABLE)
    if isinstance(exc, WrapperContractError):
        return ErrorView(texts.GENERIC_ERROR)
    if not isinstance(exc, WrapperError):
        return ErrorView(texts.GENERIC_ERROR)

    code = exc.code
    if code in MISCONFIG:
        log.error("wrapper_misconfigured", extra={"code": code, "ctx": ctx})
        return ErrorView(texts.SERVICE_UNAVAILABLE)

    specific = _CONTEXT.get(ctx, {}).get(code)
    if specific is not None:
        return specific(exc, support)
    common = _COMMON.get(code)
    if common is not None:
        return common(exc, support)
    if exc.status >= 500:
        return ErrorView(texts.SERVICE_UNAVAILABLE)
    log.error("wrapper_error_unmapped", extra={"code": code, "status": exc.status, "ctx": ctx})
    return ErrorView(texts.GENERIC_ERROR)


# --- common ---------------------------------------------------------------------------------------

_COMMON = {
    "telegram_not_linked": lambda e, s: ErrorView("Сначала привяжите аккаунт.", next="link"),
    "tenant_inactive": lambda e, s: ErrorView(f"Ваш аккаунт отключён. {_support(s)}", next="none"),
    "rate_limited": lambda e, s: ErrorView(_rate_limited(e)),
    "validation_error": lambda e, s: ErrorView("Не удалось принять введённые данные. Проверьте их и попробуйте ещё раз."),
    "payment_provider_unknown_status": lambda e, s: ErrorView(
        "Платёжная система вернула неожиданный статус. Повторите проверку через минуту."
    ),
}

# --- per context ----------------------------------------------------------------------------------


def _request_rate_limited(err: WrapperError, support: str) -> ErrorView:
    bucket = err.detail.get("bucket")
    when_text = f" Повторите через {wait(err.retry_after_s)}." if err.retry_after_s else " Повторите позже."
    if bucket == "email_request_cooldown":
        return ErrorView(f"Код уже был отправлен только что.{when_text}")
    return ErrorView(f"Достигнут лимит запросов кода на письмо.{when_text}")


_LINK_REQUEST = {
    "invalid_email": lambda e, s: ErrorView("Этот email не подошёл. Проверьте адрес и отправьте ещё раз."),
    "telegram_already_linked": lambda e, s: ErrorView(
        "Этот Telegram уже привязан к аккаунту. Посмотреть: /status", next="none"
    ),
    "rate_limited": _request_rate_limited,
    "email_delivery_failed": lambda e, s: ErrorView(
        "Не удалось отправить письмо с кодом. Попробуйте ещё раз чуть позже."
    ),
}


def _verify_rate_limited(err: WrapperError, support: str) -> ErrorView:
    when_text = f" Повторите через {wait(err.retry_after_s)}." if err.retry_after_s else " Повторите позже."
    return ErrorView(f"Слишком много неверных попыток ввода кода.{when_text}")


_LINK_VERIFY = {
    "no_pending_code": lambda e, s: ErrorView(
        "Код не найден или истёк. Запросите новый код.", next="restart"
    ),
    "code_invalid": lambda e, s: ErrorView(
        "Неверный код."
        + (
            f" Осталось попыток: {e.detail['attempts_remaining']}."
            if isinstance(e.detail.get("attempts_remaining"), int)
            else ""
        )
        + " Введите код ещё раз."
    ),
    "code_attempts_exceeded": lambda e, s: ErrorView(
        "Слишком много неверных попыток для этого кода. Запросите новый код.", next="restart"
    ),
    "rate_limited": _verify_rate_limited,
    "telegram_already_linked": lambda e, s: ErrorView(
        "Этот Telegram уже привязан к аккаунту. Посмотреть: /status", next="none"
    ),
    "tenant_already_linked": lambda e, s: ErrorView(
        f"Этот аккаунт уже привязан к другому Telegram. {_support(s)}", next="none"
    ),
    "email_ambiguous": lambda e, s: ErrorView(
        f"Не удалось однозначно определить аккаунт по этому email. {_support(s)}", next="none"
    ),
}

_PROMO = {
    "promo_invalid": lambda e, s: ErrorView("Такого промокода нет или он не активен."),
    "promo_expired": lambda e, s: ErrorView("Срок действия промокода истёк."),
    "promo_exhausted": lambda e, s: ErrorView("Промокод уже использован максимальное число раз."),
    "promo_rate_limited": lambda e, s: ErrorView(
        "Слишком много неудачных попыток ввода промокода."
        + (f" Повторите через {wait(e.retry_after_s)}." if e.retry_after_s else " Повторите позже.")
    ),
}


def _tariff_mismatch(err: WrapperError, support: str) -> ErrorView:
    """The wrapper's 409 (the fallback when the pre-check in handlers/tariffs.py did not catch it)."""
    expires = err.detail.get("tariff_expires_at")
    parsed: dt.datetime | None = None
    if isinstance(expires, str):
        try:
            parsed = dt.datetime.fromisoformat(expires)
        except ValueError:
            parsed = None
    return ErrorView(texts.active_other_tariff(parsed), next="none")


_ORDER = {
    **_PROMO,
    "tariff_id_required": lambda e, s: ErrorView("Выберите тариф: /tariffs"),
    "tariff_not_found": lambda e, s: ErrorView("Этот тариф недоступен. Откройте список заново: /tariffs"),
    "active_tariff_mismatch": _tariff_mismatch,
    "checkout_in_progress": lambda e, s: ErrorView(
        "Платёж уже создаётся. Подождите несколько секунд и нажмите кнопку ещё раз."
    ),
    "tariff_misconfigured": lambda e, s: ErrorView(
        "Этот тариф сейчас недоступен для оплаты. Попробуйте позже или выберите другой."
    ),
}

_PAYMENT = {
    "payment_not_found": lambda e, s: ErrorView(
        f"Платёж не найден для вашего аккаунта. {_support(s)}", next="none"
    ),
}

_WITHDRAWAL = {
    "withdrawal_below_minimum": lambda e, s: ErrorView(
        "Сумма меньше минимальной"
        + (
            f": минимум {rub(e.detail['minimum_rub'])}."
            if isinstance(e.detail.get("minimum_rub"), int)
            else "."
        )
        + " Введите другую сумму."
    ),
    "insufficient_balance": lambda e, s: ErrorView(
        "Недостаточно средств для вывода"
        + (
            f": доступно {rub(e.detail['available_rub'])}."
            if isinstance(e.detail.get("available_rub"), int)
            else "."
        )
        + " Введите другую сумму."
    ),
    "withdrawal_already_open": lambda e, s: ErrorView(
        "У вас уже есть открытая заявка на вывод. Новую можно создать после её обработки.",
        next="none",
    ),
}

_CONTEXT = {
    "link_request": _LINK_REQUEST,
    "link_verify": _LINK_VERIFY,
    "quote": _ORDER,
    "checkout": _ORDER,
    "payment": _PAYMENT,
    "withdrawal": _WITHDRAWAL,
}

# Every code the wrapper documents for the routes the bot uses; tests/test_errors.py pins each one to a
# non-generic message in its context.
DOCUMENTED_CODES: dict[str, tuple[str, ...]] = {
    "link_request": ("invalid_email", "telegram_already_linked", "rate_limited", "email_delivery_failed"),
    "link_verify": (
        "no_pending_code",
        "code_invalid",
        "code_attempts_exceeded",
        "rate_limited",
        "telegram_already_linked",
        "tenant_already_linked",
        "email_ambiguous",
        "tenant_inactive",
    ),
    "quote": (
        "promo_invalid",
        "promo_expired",
        "promo_exhausted",
        "promo_rate_limited",
        "tariff_id_required",
        "tariff_not_found",
        "telegram_not_linked",
        "tenant_inactive",
        "rate_limited",
    ),
    "checkout": (
        "active_tariff_mismatch",
        "promo_invalid",
        "promo_expired",
        "promo_exhausted",
        "promo_rate_limited",
        "checkout_in_progress",
        "return_url_not_allowed",
        "tariff_not_found",
        "tariff_misconfigured",
        "telegram_not_linked",
        "tenant_inactive",
        "rate_limited",
    ),
    "payment": ("payment_not_found", "payment_provider_unknown_status", "telegram_not_linked", "rate_limited"),
    "withdrawal": (
        "withdrawal_below_minimum",
        "insufficient_balance",
        "withdrawal_already_open",
        "telegram_not_linked",
        "rate_limited",
    ),
}
