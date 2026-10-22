"""Tariffs from the API only, the link requirement, promo -> quote -> checkout, double-tap, error mapping."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.conftest import UID, Harness
from tests.fake_wrapper import tariff
from tests.mocked_telegram import BOT_USERNAME, buttons
from ytgen_bot import callbacks, texts
from ytgen_bot.formatting import rub


async def test_tariffs_render_only_what_the_api_returns(consented: Harness):
    h = consented
    h.fake.tariffs = [tariff(31, "Тест-Альфа", 777, period_days=14), tariff(32, "Тест-Бета", 98765, period_days=1)]
    await h.say("/tariffs")
    assert "Тест-Альфа" in h.last and rub(777) in h.last and "14 дн." in h.last
    assert "Тест-Бета" in h.last and rub(98765) in h.last
    assert "Старт" not in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.tariff(31), callbacks.tariff(32)]
    assert h.fake.calls("GET", "/tariffs")


async def test_tariffs_work_before_account_link(consented: Harness):
    await consented.say("/tariffs")
    assert "Старт" in consented.last


async def test_capacity_lines_come_from_the_api(consented: Harness):
    h = consented
    t = tariff(41, "Неделя", 100, period_days=7)
    t["capacity"].update(window_hours=168, ai_video_videos_per_window=2, image_stock_videos_per_day=None,
                         image_stock_videos_per_window=9)
    t["max_ai_scenes_per_video"] = 6
    h.fake.tariffs = [t]
    await h.say("/tariffs")
    assert "до 2 за 7 дн." in h.last
    assert "до 9 за 7 дн." in h.last
    assert "до 6" in h.last


async def test_empty_tariff_list_is_said_plainly(consented: Harness):
    consented.fake.tariffs = []
    await consented.say("/tariffs")
    assert "нет доступных тарифов" in consented.last


async def test_tariff_requires_a_linked_account_first(consented: Harness):
    h = consented
    await h.press(callbacks.tariff(11))
    assert h.last == texts.link_need_account()
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(11)]
    assert not h.fake.calls("POST", "/billing/checkout") and not h.fake.calls("POST", "/billing/quote")


async def test_unknown_tariff_id_is_handled(linked: Harness):
    await linked.press(callbacks.tariff(999))
    assert "недоступен" in linked.last


async def test_full_flow_without_promo(linked: Harness):
    h = linked
    await h.press(callbacks.tariff(11))
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.promo(11), callbacks.quote(11)]
    await h.press(callbacks.quote(11))
    assert h.fake.calls("POST", "/billing/quote")[-1]["json"] == {"tariff_id": 11}
    assert f"К оплате: <b>{rub(1234)}</b>" in h.last
    pay = buttons(h.session.last_markup())[0]
    assert pay.callback_data == callbacks.pay(11, 1234)
    await h.press(pay.callback_data)
    body = h.fake.calls("POST", "/billing/checkout")[-1]["json"]
    assert body == {"tariff_id": 11, "return_url": f"https://t.me/{BOT_USERNAME}?start=paid"}
    url_button, check_button = buttons(h.session.last_markup())
    assert url_button.url.startswith("https://pay.example.test/checkout/")
    assert check_button.callback_data.startswith("chk:")
    assert "Платёж обрабатывается" in h.last


async def test_full_flow_with_promo_shows_the_payable_amount_and_sends_the_code(linked: Harness):
    h = linked
    await h.press(callbacks.promo(11))
    await h.say("save10xx")  # normalised to upper case
    quote_body = h.fake.calls("POST", "/billing/quote")[-1]["json"]
    assert quote_body == {"tariff_id": 11, "promo_code": "SAVE10XX"}
    payable = 1234 - 1234 * 10 // 100
    assert f"К оплате: <b>{rub(payable)}</b>" in h.last
    assert "−" in h.last and "10%" in h.last
    pay = buttons(h.session.last_markup())[0]
    assert pay.callback_data == callbacks.pay(11, payable, "SAVE10XX")
    await h.press(pay.callback_data)
    assert h.fake.calls("POST", "/billing/checkout")[-1]["json"]["promo_code"] == "SAVE10XX"
    assert rub(payable) in h.last


async def test_pay_button_works_after_a_restart(linked: Harness):
    """Everything it needs is in the callback data; no FSM or cache involved."""
    h = linked
    await h.press(callbacks.pay(11, 1234, "SAVE10XX"))
    assert h.fake.calls("POST", "/billing/checkout")[-1]["json"]["promo_code"] == "SAVE10XX"


@pytest.mark.parametrize("bad", ["", "ab cd", "промо", "x" * 40, "a;b"])
async def test_bad_promo_format_is_rejected_locally(linked: Harness, bad: str):
    h = linked
    await h.press(callbacks.promo(11))
    await h.say(bad or " ")
    assert h.last == texts.promo_bad_format()
    assert not h.fake.calls("POST", "/billing/quote")


async def test_unknown_promo_from_the_wrapper(linked: Harness):
    h = linked
    await h.press(callbacks.promo(11))
    await h.say("NOPE1234")
    assert "Такого промокода нет" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.promo(11), callbacks.quote(11)]


@pytest.mark.parametrize(
    "code,status,expect",
    [
        ("promo_expired", 400, "истёк"),
        ("promo_exhausted", 400, "использован"),
        ("promo_rate_limited", 429, "Слишком много неудачных попыток"),
    ],
)
async def test_promo_errors_on_quote_and_checkout(linked: Harness, code, status, expect):
    h = linked
    h.fake.queue_error("POST", "/billing/quote", status, code, retry_after_s=120)
    await h.press(callbacks.promo(11))
    await h.say("SAVE10XX")
    assert expect in h.last
    h.fake.queue_error("POST", "/billing/checkout", status, code, retry_after_s=120)
    await h.press(callbacks.pay(11, 1000, "SAVE10XX"))
    assert expect in h.last


async def test_active_tariff_mismatch_message(linked: Harness):
    h = linked
    h.fake.queue_error(
        "POST", "/billing/checkout", 409, "active_tariff_mismatch",
        active_tariff_id=12, tariff_expires_at="2026-11-21T10:00:00+00:00",
    )
    await h.press(callbacks.pay(11, 1234))
    assert "другой тариф" in h.last and "21.11.2026" in h.last


async def test_checkout_in_progress_message(linked: Harness):
    h = linked
    h.fake.queue_error("POST", "/billing/checkout", 409, "checkout_in_progress", retry_after_s=3)
    await h.press(callbacks.pay(11, 1234))
    assert "уже создаётся" in h.last


async def test_unknown_tariff_is_mapped_explicitly(linked: Harness, caplog):
    """Bot routes answer 404 `{"detail": {"error": "tariff_not_found"}}` for an unknown tariff."""
    h = linked
    await h.press(callbacks.pay(99, 1234))
    assert "Этот тариф недоступен" in h.last
    await h.press(callbacks.quote(99))
    assert "Этот тариф недоступен" in h.last
    assert not any(r.message == "wrapper_error_unmapped" for r in caplog.records)


async def test_a_plain_string_404_is_no_longer_special_cased(linked: Harness, caplog):
    """Generic handling for shapes the contract does not describe: generic message + an ERROR log."""
    h = linked
    h.fake.queue("POST", "/billing/checkout", httpx.Response(404, json={"detail": "Tariff not found."}))
    await h.press(callbacks.pay(99, 1234))
    assert h.last == texts.GENERIC_ERROR
    assert any(r.message == "wrapper_error_unmapped" for r in caplog.records)


async def test_not_linked_on_checkout_offers_linking(consented: Harness):
    h = consented
    await h.press(callbacks.pay(11, 1234))
    assert "привяжите аккаунт" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(11)]


async def test_misconfigured_return_url_is_loud_and_generic(linked: Harness, caplog):
    h = linked
    h.fake.queue_error("POST", "/billing/checkout", 422, "return_url_not_allowed")
    await h.press(callbacks.pay(11, 1234))
    assert h.last == texts.SERVICE_UNAVAILABLE
    assert any(r.message == "wrapper_misconfigured" for r in caplog.records)


async def test_reused_payment_says_so_and_creates_nothing_new(linked: Harness):
    h = linked
    pid = h.fake.new_payment()
    h.fake.queue("POST", "/billing/checkout", httpx.Response(200, json={
        "payment_id": pid, "payment_url": "https://pay.example.test/old", "amount_rub": 1234,
        "tariff_id": 11, "reused": True,
    }))
    await h.press(callbacks.pay(11, 1234))
    assert "Ранее созданная ссылка" in h.last and "новый платёж не создаётся" in h.last
    assert buttons(h.session.last_markup())[0].url == "https://pay.example.test/old"


async def test_amount_differing_from_the_quote_is_called_out(linked: Harness):
    h = linked
    pid = h.fake.new_payment()
    h.fake.queue("POST", "/billing/checkout", httpx.Response(200, json={
        "payment_id": pid, "payment_url": "https://pay.example.test/x", "amount_rub": 2000,
        "tariff_id": 11, "reused": False,
    }))
    await h.press(callbacks.pay(11, 1234))
    assert rub(2000) in h.last and rub(1234) in h.last and "Внимание" in h.last


async def test_non_https_payment_url_is_refused(linked: Harness, caplog):
    h = linked
    pid = h.fake.new_payment()
    h.fake.queue("POST", "/billing/checkout", httpx.Response(200, json={
        "payment_id": pid, "payment_url": "http://insecure.example/x", "amount_rub": 1234,
        "tariff_id": 11, "reused": False,
    }))
    await h.press(callbacks.pay(11, 1234))
    assert h.last == texts.GENERIC_ERROR
    assert any(r.message == "checkout_payment_url_not_https" for r in caplog.records)


async def test_double_tap_on_pay_creates_one_checkout(linked: Harness):
    h = linked
    await asyncio.gather(h.press(callbacks.pay(11, 1234)), h.press(callbacks.pay(11, 1234)))
    assert len(h.fake.calls("POST", "/billing/checkout")) == 1
    assert texts.ALREADY_PROCESSING in [a.text for a in h.session.answers()]


async def test_second_tap_right_after_the_first_is_debounced(linked: Harness):
    h = linked
    await h.press(callbacks.pay(11, 1234))
    await h.press(callbacks.pay(11, 1234))
    assert len(h.fake.calls("POST", "/billing/checkout")) == 1


async def test_a_deliberate_retry_after_the_cooldown_goes_through(linked: Harness):
    h = linked
    clock = [0.0]
    h.ctx.guard._clock = lambda: clock[0]
    await h.press(callbacks.pay(11, 1234))
    clock[0] += 10
    await h.press(callbacks.pay(11, 1234))
    assert len(h.fake.calls("POST", "/billing/checkout")) == 2  # the wrapper's own reuse handles this


async def test_wrapper_unreachable_on_checkout_creates_no_watcher(linked: Harness):
    h = linked
    h.fake.queue("POST", "/billing/checkout", httpx.ConnectError("down"))
    await h.press(callbacks.pay(11, 1234))
    assert h.last == texts.SERVICE_UNAVAILABLE
    assert not h.ctx.watchers._tasks


# --- an active DIFFERENT tariff: the quote's own flag (wrapper a36c9ac), no pay button ---------------

ACTIVE = {"tariff_id": 12, "tariff_name": "Профи", "tariff_expires_at": "2026-11-21T10:00:00Z"}


def quotes(h: Harness) -> list[dict]:
    return [c["json"] for c in h.fake.calls("POST", "/billing/quote")]


async def test_mismatch_flag_shows_the_message_and_no_quote_or_pay_button(linked: Harness):
    h = linked
    h.fake.linked[UID].update(ACTIVE)
    await h.press(callbacks.quote(11))
    assert "другой тариф" in h.last and "21.11.2026" in h.last  # the date comes from the quote
    assert "К оплате" not in h.last and buttons(h.session.last_markup()) == []
    assert len(quotes(h)) == 1  # the quote that was needed anyway: no extra calls
    assert not h.fake.calls("POST", "/billing/checkout")


async def test_mismatch_flag_on_the_promo_path(linked: Harness):
    h = linked
    h.fake.linked[UID].update(ACTIVE)
    await h.press(callbacks.promo(11))
    await h.say("SAVE10XX")
    assert quotes(h) == [{"tariff_id": 11, "promo_code": "SAVE10XX"}]
    assert "другой тариф" in h.last and buttons(h.session.last_markup()) == []


async def test_flag_false_renewal_of_the_own_tariff_keeps_the_pay_flow(linked: Harness):
    h = linked
    h.fake.linked[UID].update(ACTIVE)
    await h.press(callbacks.tariff(12))  # no pre-check calls at all
    assert quotes(h) == []
    await h.press(callbacks.quote(12))
    assert len(quotes(h)) == 1 and "К оплате" in h.last
    await h.press(buttons(h.session.last_markup())[0].callback_data)
    assert h.fake.calls("POST", "/billing/checkout")


async def test_flag_false_for_the_wrappers_successor(linked: Harness):
    """The wrapper's predicate excludes the successor; the bot just follows the flag."""
    h = linked
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z")
    h.fake.successors[11] = 12
    await h.press(callbacks.quote(12))
    assert "К оплате" in h.last
    await h.press(buttons(h.session.last_markup())[0].callback_data)
    assert h.fake.calls("POST", "/billing/checkout")


async def test_lapsed_or_missing_tariff_gets_the_normal_quote(linked: Harness):
    h = linked
    h.fake.linked[UID].update(tariff_id=12, tariff_name="Профи", tariff_expires_at="2026-09-01T10:00:00Z")
    await h.press(callbacks.quote(11))
    assert "К оплате" in h.last
    h.fake.linked[UID].update(tariff_id=None, tariff_name=None, tariff_expires_at=None)
    await h.press(callbacks.quote(11))
    assert "К оплате" in h.last


async def test_an_older_wrapper_without_the_field_reads_as_false_and_the_409_is_the_fallback(linked: Harness):
    h = linked
    h.fake.linked[UID].update(ACTIVE)
    h.fake.queue("POST", "/billing/quote", httpx.Response(200, json={
        "tariff_id": 11, "plan_name": "Старт", "price_rub": 1234, "period_days": 30, "promo": None,
    }))  # no active_tariff_mismatch key
    await h.press(callbacks.quote(11))
    assert "К оплате" in h.last  # fail safe: not blocked by a missing field
    await h.press(buttons(h.session.last_markup())[0].callback_data)  # checkout answers the real 409
    assert "другой тариф" in h.last and "21.11.2026" in h.last and not h.fake.payments


async def test_state_changed_after_the_quote_the_409_is_the_fallback(linked: Harness):
    h = linked
    await h.press(callbacks.quote(11))  # free to buy at this moment
    assert "К оплате" in h.last
    h.fake.linked[UID].update(ACTIVE)  # ...then a payment elsewhere activates another tariff
    await h.press(buttons(h.session.last_markup())[0].callback_data)
    assert "другой тариф" in h.last and not h.fake.payments


async def test_link_continuation_ends_at_the_tariff_screen_without_extra_quotes(consented: Harness):
    h = consented
    await h.press(callbacks.tariff(11))
    assert quotes(h) == []
