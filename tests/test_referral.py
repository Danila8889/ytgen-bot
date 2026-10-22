"""Referral screen (linked users only), the deep link, rules from the API, and withdrawals."""
from __future__ import annotations

import pytest

from tests.conftest import SUPPORT, Harness
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks, texts
from ytgen_bot.formatting import rub


async def test_referral_requires_a_linked_account(consented: Harness):
    h = consented
    await h.say("/referral")
    assert "привяжите аккаунт" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(0)]


async def test_screen_is_built_from_api_fields_only(linked: Harness):
    h = linked
    info = h.fake.referral_info
    info["balance"].update(pending_rub=111, available_rub=777, requested_rub=21, paid_out_rub=55)
    await h.say("/referral")
    t = h.last
    assert info["deep_link"] in t
    assert "Вознаграждение: 13%" in t and "Удержание: 3 дн." in t and f"Минимальный вывод: {rub(250)}" in t
    for amount in (111, 777, 21, 55):
        assert rub(amount) in t
    assert "Переходов по ссылке: 4" in t and "Привязано пользователей: 2" in t and "Оплатили: 1" in t
    assert "https://ytgenerator.ru/legal/oferta#section-10" in t
    assert "К удержанию" not in t  # owed_rub is zero


async def test_owed_is_shown_when_non_zero(linked: Harness):
    h = linked
    h.fake.referral_info["balance"]["owed_rub"] = 321
    await h.say("/referral")
    assert "К удержанию из будущих начислений" in h.last and rub(321) in h.last


async def test_missing_deep_link_is_loud_and_has_no_fallback(linked: Harness, caplog):
    h = linked
    h.fake.referral_info["deep_link"] = None
    await h.say("/referral")
    assert "временно недоступна" in h.last
    assert "t.me" not in h.last and "ref_" not in h.last
    errors = [r for r in caplog.records if r.message == "referral_deep_link_missing"]
    assert errors and errors[0].levelname == "ERROR"


async def test_withdraw_button_only_when_possible(linked: Harness):
    h = linked
    await h.say("/referral")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.withdraw("start")]
    h.fake.referral_info["balance"]["available_rub"] = 10  # below the API's minimum
    await h.say("/referral")
    assert buttons(h.session.last_markup()) == []
    h.fake.referral_info["balance"]["available_rub"] = 777
    h.fake.referral_info["open_withdrawal"] = {"id": 3, "amount_rub": 300, "created_at": "2026-10-20T10:00:00Z"}
    await h.say("/referral")
    assert buttons(h.session.last_markup()) == []
    assert "Заявка на вывод №3" in h.last and SUPPORT in h.last


async def test_withdrawal_happy_path(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw("start"))
    assert rub(250) in h.last and rub(777) in h.last
    assert "карту" in h.last  # told NOT to send payout details here
    await h.say("500")
    assert h.last == texts.withdraw_confirm(500)
    await h.press(callbacks.withdraw_ok(500))
    assert h.fake.calls("POST", "/referral/withdrawals")[-1]["json"] == {"amount_rub": 500}
    assert "Оператор свяжется с вами в Telegram" in h.last and SUPPORT in h.last
    assert rub(500) in h.last


async def test_no_payout_details_are_asked_for(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw("start"))
    await h.say("500")
    await h.press(callbacks.withdraw_ok(500))
    everything = " ".join(h.texts).lower()
    for word in ("номер карты", "введите карту", "введите номер телефона", "реквизиты:"):
        assert word not in everything
    body = h.fake.calls("POST", "/referral/withdrawals")[-1]["json"]
    assert set(body) == {"amount_rub"}


@pytest.mark.parametrize("bad", ["abc", "12.5", "-5", "0", "1e3", "", "99999999999"])
async def test_amount_must_be_a_positive_whole_number(linked: Harness, bad: str):
    h = linked
    await h.press(callbacks.withdraw("start"))
    await h.say(bad or " ")
    assert h.last == texts.withdraw_bad_amount()
    assert not h.fake.calls("POST", "/referral/withdrawals")


async def test_amount_with_spaces_is_accepted(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw("start"))
    await h.say("1 000")
    assert h.last == texts.withdraw_confirm(1000)


async def test_below_minimum_error_lets_the_user_enter_another_amount(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw_ok(100))
    assert "минимум" in h.last and rub(250) in h.last
    await h.say("300")  # the amount prompt is active again
    assert h.last == texts.withdraw_confirm(300)


async def test_insufficient_balance(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw_ok(5000))
    assert "Недостаточно средств" in h.last and rub(777) in h.last


async def test_already_open_withdrawal(linked: Harness):
    h = linked
    h.fake.referral_info["open_withdrawal"] = {"id": 3, "amount_rub": 300, "created_at": "2026-10-20T10:00:00Z"}
    await h.press(callbacks.withdraw_ok(300))
    assert "уже есть открытая заявка" in h.last


async def test_withdrawal_rate_limited(linked: Harness):
    h = linked
    h.fake.queue_error("POST", "/referral/withdrawals", 429, "rate_limited", bucket="withdrawal_user", retry_after_s=1800)
    await h.press(callbacks.withdraw_ok(300))
    assert "30 мин" in h.last


async def test_double_tap_on_confirm_requests_once(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw_ok(300))
    await h.press(callbacks.withdraw_ok(300))
    assert len(h.fake.calls("POST", "/referral/withdrawals")) == 1


async def test_nothing_to_withdraw(linked: Harness):
    h = linked
    h.fake.referral_info["balance"]["available_rub"] = 0
    await h.press(callbacks.withdraw("start"))
    assert h.last == texts.withdraw_unavailable()


async def test_cancel_clears_the_flow(linked: Harness):
    h = linked
    await h.press(callbacks.withdraw("start"))
    await h.press(callbacks.withdraw("cancel"))
    await h.say("500")
    assert h.last == texts.UNKNOWN_MESSAGE
    assert not h.fake.calls("POST", "/referral/withdrawals")


async def test_support_contact_in_help(consented: Harness):
    await consented.say("/help")
    assert SUPPORT in consented.last
    assert "не используется" in consented.last and "ytgenerator.ru" in consented.last
