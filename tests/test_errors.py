"""Every documented wrapper error code maps to a clear, non-generic Russian message in its context."""
from __future__ import annotations

import logging

import pytest

from ytgen_bot import texts
from ytgen_bot.errors_ru import DOCUMENTED_CODES, MISCONFIG, describe
from ytgen_bot.wrapper import WrapperContractError, WrapperError, WrapperUnavailable

GENERIC = {texts.GENERIC_ERROR, texts.SERVICE_UNAVAILABLE}
EXTRA = {
    "code_invalid": {"attempts_remaining": 3},
    "withdrawal_below_minimum": {"minimum_rub": 250},
    "insufficient_balance": {"available_rub": 777},
    "active_tariff_mismatch": {"active_tariff_id": 1, "tariff_expires_at": "2026-11-21T10:00:00+00:00"},
}
STATUS = {"http_404": 404}


def make(code: str) -> WrapperError:
    status = STATUS.get(code, 400)
    detail = dict(EXTRA.get(code, {}))
    retry = 90 if code in ("rate_limited", "promo_rate_limited") else None
    return WrapperError(status, code, detail, retry)


CASES = [(ctx, code) for ctx, codes in DOCUMENTED_CODES.items() for code in codes]


@pytest.mark.parametrize("ctx,code", CASES, ids=[f"{c}:{k}" for c, k in CASES])
def test_documented_code_has_a_specific_message(ctx: str, code: str):
    view = describe(make(code), ctx, "@support")
    if code in MISCONFIG:  # operator-side: generic for the user, loud in the logs (tested below)
        assert view.text == texts.SERVICE_UNAVAILABLE
        return
    assert view.text and view.text not in GENERIC
    assert "{" not in view.text and "None" not in view.text


def test_every_documented_route_error_from_the_task_is_covered():
    covered = {code for codes in DOCUMENTED_CODES.values() for code in codes}
    required = {
        # link
        "code_invalid", "no_pending_code", "code_attempts_exceeded", "telegram_already_linked",
        "tenant_already_linked", "email_ambiguous", "email_delivery_failed", "rate_limited", "invalid_email",
        # billing
        "active_tariff_mismatch", "promo_invalid", "promo_expired", "promo_exhausted", "promo_rate_limited",
        "checkout_in_progress", "return_url_not_allowed", "telegram_not_linked", "payment_not_found",
        # referral
        "withdrawal_below_minimum", "insufficient_balance", "withdrawal_already_open",
    }
    assert required <= covered


def test_details_are_rendered():
    assert "Осталось попыток: 3" in describe(make("code_invalid"), "link_verify", "s").text
    assert "250" in describe(make("withdrawal_below_minimum"), "withdrawal", "s").text
    assert "777" in describe(make("insufficient_balance"), "withdrawal", "s").text
    assert "21.11.2026" in describe(make("active_tariff_mismatch"), "checkout", "s").text
    assert "1 мин" not in describe(make("rate_limited"), "checkout", "s").text  # 90 s -> 2 мин
    assert "2 мин" in describe(make("rate_limited"), "checkout", "s").text


def test_rate_limited_without_a_time_is_still_polite():
    text = describe(WrapperError(429, "rate_limited", {}, None), "checkout", "s").text
    assert "позже" in text


def test_support_contact_is_html_escaped():
    text = describe(WrapperError(403, "tenant_inactive"), "link_verify", "<b>@x</b>").text
    assert "<b>@x</b>" not in text and "&lt;b&gt;" in text


@pytest.mark.parametrize("code", sorted(MISCONFIG))
def test_operator_side_errors_are_loud_and_generic_for_the_user(code, caplog):
    caplog.set_level(logging.ERROR)
    view = describe(WrapperError(503, code), "checkout", "s")
    assert view.text == texts.SERVICE_UNAVAILABLE
    assert any(r.message == "wrapper_misconfigured" and r.levelno == logging.ERROR for r in caplog.records)


def test_unmapped_code_is_logged_at_error_not_swallowed(caplog):
    caplog.set_level(logging.ERROR)
    view = describe(WrapperError(400, "something_new"), "checkout", "s")
    assert view.text == texts.GENERIC_ERROR
    assert any(r.message == "wrapper_error_unmapped" for r in caplog.records)


def test_server_errors_say_try_again():
    assert describe(WrapperError(500, "http_500"), "checkout", "s").text == texts.SERVICE_UNAVAILABLE
    assert describe(WrapperError(502, "payment_provider_unknown_status"), "payment", "s").text != texts.GENERIC_ERROR


def test_transport_and_contract_failures():
    assert describe(WrapperUnavailable("ConnectError"), "checkout", "s").text == texts.SERVICE_UNAVAILABLE
    assert describe(WrapperContractError("x"), "checkout", "s").text == texts.GENERIC_ERROR
    assert describe(RuntimeError("x"), "checkout", "s").text == texts.GENERIC_ERROR
