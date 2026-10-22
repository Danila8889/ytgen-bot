"""Account link: email -> code -> verified; every documented error has a clear message and a way to retry."""
from __future__ import annotations

import logging

import pytest

from tests.conftest import UID, Harness
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks, texts
from ytgen_bot.formatting import rub

EMAIL = "john.smith@example.com"


async def begin(h: Harness, tariff_id: int = 0) -> None:
    await h.press(callbacks.link(tariff_id))


async def test_happy_path_links_and_shows_menu(consented: Harness):
    h = consented
    await begin(h)
    assert "email" in h.last.lower()
    await h.say(EMAIL)
    assert h.fake.calls("POST", "/link/request-code")[-1]["json"] == {"email": EMAIL}
    assert "j***@example.com" in h.last and EMAIL not in h.last
    assert "10 мин" in h.last
    await h.say("123 456")  # spaces tolerated
    assert h.fake.calls("POST", "/link/verify")[-1]["json"] == {"code": "123456"}
    assert "аккаунт создан и привязан" in h.texts[-2]
    assert UID in h.fake.linked


async def test_bad_email_never_reaches_the_wrapper(consented: Harness):
    h = consented
    await begin(h)
    for bad in ["not-an-email", "a@b", "x" * 300 + "@example.com", "a b@example.com"]:
        await h.say(bad)
        assert h.last == texts.link_bad_email()
    assert not h.fake.calls("POST", "/link/request-code")


async def test_bad_code_format_never_reaches_the_wrapper(consented: Harness):
    h = consented
    await begin(h)
    await h.say(EMAIL)
    for bad in ["12345", "abcdef", "1234567", "12 34"]:
        await h.say(bad)
        assert h.last == texts.link_bad_code_format()
    assert not h.fake.calls("POST", "/link/verify")


async def test_wrong_code_shows_attempts_and_lets_the_user_retry(consented: Harness):
    h = consented
    await begin(h)
    await h.say(EMAIL)
    await h.say("999999")
    assert "Неверный код" in h.last and "Осталось попыток: 4" in h.last
    await h.say("123456")  # state kept: the right code still works
    assert UID in h.fake.linked


async def test_expired_or_missing_code_offers_a_new_one(consented: Harness):
    h = consented
    await begin(h, tariff_id=11)
    await h.say(EMAIL)
    h.fake.queue_error("POST", "/link/verify", 400, "no_pending_code")
    await h.say("123456")
    assert "Код не найден или истёк" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(11)]


async def test_attempts_exceeded_offers_a_new_code(consented: Harness):
    h = consented
    await begin(h)
    await h.say(EMAIL)
    h.fake.queue_error("POST", "/link/verify", 429, "code_attempts_exceeded")
    await h.say("123456")
    assert "Запросите новый код" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(0)]


async def test_rate_limited_request_shows_retry_time_and_stays_in_state(consented: Harness):
    h = consented
    await begin(h)
    h.fake.queue_error("POST", "/link/request-code", 429, "rate_limited", bucket="email_request_cooldown", retry_after_s=45)
    await h.say(EMAIL)
    assert "45 с" in h.last
    await h.say(EMAIL)  # still asking for an email: retry works
    assert "Код отправлен" in h.last


async def test_hourly_bucket_gets_minutes(consented: Harness):
    h = consented
    await begin(h)
    h.fake.queue_error("POST", "/link/request-code", 429, "rate_limited", bucket="email_request_user", retry_after_s=2400)
    await h.say(EMAIL)
    assert "40 мин" in h.last and "лимит" in h.last.lower()


async def test_email_delivery_failed_allows_retry(consented: Harness):
    h = consented
    await begin(h)
    h.fake.queue_error("POST", "/link/request-code", 502, "email_delivery_failed")
    await h.say(EMAIL)
    assert "Не удалось отправить письмо" in h.last
    await h.say(EMAIL)
    assert "Код отправлен" in h.last


@pytest.mark.parametrize(
    "code,status,expect",
    [
        ("tenant_already_linked", 409, "другому Telegram"),
        ("email_ambiguous", 409, "однозначно"),
        ("telegram_already_linked", 409, "уже привязан"),
        ("tenant_inactive", 403, "отключён"),
    ],
)
async def test_terminal_verify_errors_point_to_support_and_end_the_flow(consented: Harness, code, status, expect):
    h = consented
    await begin(h)
    await h.say(EMAIL)
    h.fake.queue_error("POST", "/link/verify", status, code)
    await h.say("123456")
    assert expect in h.last
    if code in ("tenant_already_linked", "email_ambiguous", "tenant_inactive"):
        assert "@ytgen_support_test" in h.last
    await h.say("123456")  # flow ended: not treated as a code any more
    assert h.last == texts.UNKNOWN_MESSAGE


async def test_telegram_already_linked_at_request_time(consented: Harness):
    h = consented
    h.fake.link(UID)
    await begin(h)
    await h.say(EMAIL)
    assert "уже привязан" in h.last


async def test_ambiguous_validation_error_from_the_wrapper_is_mapped(consented: Harness):
    import httpx

    h = consented
    await begin(h)
    h.fake.queue("POST", "/link/request-code", httpx.Response(422, json={"detail": [{"loc": ["body", "email"], "msg": "x", "type": "y"}]}))
    await h.say(EMAIL)
    assert "Не удалось принять введённые данные" in h.last


async def test_wrapper_down_during_link_is_a_retryable_message(consented: Harness):
    import httpx

    h = consented
    await begin(h)
    h.fake.queue("POST", "/link/request-code", httpx.ConnectError("boom"))
    await h.say(EMAIL)
    assert h.last == texts.SERVICE_UNAVAILABLE
    await h.say(EMAIL)
    assert "Код отправлен" in h.last


async def test_link_then_continues_with_the_tariff_that_started_it(consented: Harness):
    h = consented
    await h.press(callbacks.tariff(11))  # not linked yet
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(11)]
    await h.press(callbacks.link(11))
    await h.say(EMAIL)
    await h.say("123456")
    assert "Старт" in h.last and "промокод" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.promo(11), callbacks.quote(11)]


async def test_a_command_ends_the_flow(consented: Harness):
    h = consented
    await begin(h)
    await h.say("/tariffs")
    assert "Тарифы" in h.last
    await h.say(EMAIL)
    assert h.last == texts.UNKNOWN_MESSAGE
    assert not h.fake.calls("POST", "/link/request-code")


async def test_emails_and_codes_are_never_logged_in_full(consented: Harness, caplog):
    h = consented
    caplog.set_level(logging.DEBUG)
    await begin(h)
    await h.say(EMAIL)
    await h.say("654321")  # wrong
    await h.say("123456")
    dump = "\n".join(
        f"{r.getMessage()} {sorted(r.__dict__.items(), key=lambda kv: kv[0])}" for r in caplog.records
    )
    assert EMAIL not in dump and "john.smith" not in dump
    assert "123456" not in dump and "654321" not in dump
    assert "j***@example.com" in dump  # the masked form is what gets logged
    assert any(r.getMessage() == "link_code_rejected" for r in caplog.records)


async def test_double_submit_of_the_email_sends_one_request(consented: Harness):
    h = consented
    await begin(h)
    await h.say(EMAIL)
    # State is now `code`; a second email-looking message is not an email request.
    await h.say(EMAIL)
    assert len(h.fake.calls("POST", "/link/request-code")) == 1


def test_money_helper_is_used_for_amounts():
    assert rub(1234) == "1 234 ₽"
