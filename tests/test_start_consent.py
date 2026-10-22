"""/start, the consent gate, referral-code parsing, the paid return, /stop and blocked-bot handling."""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from tests.conftest import UID, Harness
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks, texts
from ytgen_bot.formatting import rub
from ytgen_bot.startparam import parse_start_param


def start_bodies(h: Harness) -> list[dict]:
    return [c["json"] for c in h.fake.calls("POST", "/start")]


# --- start parameter parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind,code",
    [
        (None, "none", None),
        ("", "none", None),
        ("paid", "paid", None),
        ("ref_ABCDEF123456", "ref", "ABCDEF123456"),
        ("ref_a-b_c", "ref", "a-b_c"),
        ("ref_", "invalid", None),
        ("ref_bad code", "invalid", None),
        ("ref_" + "x" * 61, "invalid", None),
        ("whatever", "invalid", None),
        ("paid_extra", "invalid", None),
    ],
)
def test_parse_start_param(raw, kind, code):
    param = parse_start_param(raw)
    assert (param.kind, param.ref_code) == (kind, code)


# --- consent gate --------------------------------------------------------------------------------


async def test_new_user_sees_consent_with_all_three_links_and_no_consent_is_sent(h: Harness):
    await h.say("/start")
    text, markup = h.last, h.session.last_markup()
    assert "https://ytgenerator.ru/legal/oferta#section-10" in text
    assert 'href="https://ytgenerator.ru/legal/oferta"' in text
    assert "https://ytgenerator.ru/legal/privacy#section-11" in text
    assert [b.callback_data for b in buttons(markup)] == ["cn"]
    # The only call so far reads state; nothing claims consent before the button.
    assert start_bodies(h) == [{"consent": False, "language": "ru"}]
    assert h.fake.user(UID)["consent_at"] is None


@pytest.mark.parametrize("command", ["/tariffs", "/referral", "/status", "/help", "/me", "random text"])
async def test_nothing_works_before_consent(h: Harness, command: str):
    await h.say(command)
    assert "Согласен" in [b.text for b in buttons(h.session.last_markup())]
    # No tariffs / referral / me call was made on behalf of an unconsented user.
    assert {c["path"] for c in h.fake.requests} <= {"/start"}


async def test_callbacks_are_gated_too(h: Harness):
    await h.press(callbacks.menu("tariffs"))
    await h.press(callbacks.tariff(11))
    await h.press(callbacks.check("pay-1"))
    assert {c["path"] for c in h.fake.requests} <= {"/start"}
    assert texts.CONSENT_FIRST in [a.text for a in h.session.answers()]


async def test_consent_button_sends_consent_true_then_menu(h: Harness):
    await h.say("/start")
    await h.press(callbacks.consent())
    assert start_bodies(h)[-1] == {"consent": True, "language": "ru"}
    assert h.fake.user(UID)["consent_at"] is not None
    assert "Сам сервис в Telegram не используется" in h.last
    assert "Тарифы и оплата" in [b.text for b in buttons(h.session.last_markup())]
    # Gate is open now.
    await h.say("/tariffs")
    assert "Старт" in h.last and rub(1234) in h.last


async def test_double_tap_on_consent_calls_once(h: Harness):
    await h.press(callbacks.consent())
    await h.press(callbacks.consent())
    assert [b for b in start_bodies(h) if b.get("consent")] == [{"consent": True, "language": "ru"}]
    assert texts.ALREADY_PROCESSING in [a.text for a in h.session.answers()]


async def test_consent_survives_a_restart_without_asking_again(h: Harness):
    """Cold process cache, consent stored in the wrapper: one state read, then the command runs."""
    h.fake.consent(UID)
    await h.say("/tariffs")
    assert "Старт" in h.last
    assert start_bodies(h) == [{"consent": False, "language": "ru"}]
    await h.say("/tariffs")  # now cached: no second read
    assert len(start_bodies(h)) == 1


async def test_opt_out_requires_consent_again(h: Harness):
    h.fake.consent(UID)
    h.ctx.consent.set(UID, True)
    await h.say("/stop")
    assert h.fake.user(UID)["opted_out_at"] is not None
    await h.say("/tariffs")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]
    await h.press(callbacks.consent())
    assert h.fake.user(UID)["opted_out_at"] is None
    await h.say("/tariffs")
    assert "Старт" in h.last


async def test_cold_cache_with_opted_out_user_is_not_consented(h: Harness):
    h.fake.consent(UID)
    h.fake.user(UID)["opted_out_at"] = "2026-10-01T00:00:00Z"
    await h.say("/help")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]


# --- referral code parsing -----------------------------------------------------------------------


async def test_ref_param_is_held_in_the_consent_button_and_sent_only_after_it(h: Harness):
    await h.say("/start ref_ABCDEF123456")
    assert all("referral_code" not in b for b in start_bodies(h))
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn:ABCDEF123456"]
    await h.press("cn:ABCDEF123456")
    assert start_bodies(h)[-1] == {"consent": True, "language": "ru", "referral_code": "ABCDEF123456"}
    assert "Реферальная ссылка принята" in h.texts[-1]


async def test_ref_param_for_an_already_started_user_is_still_sent(h: Harness):
    """Organic start earlier (consented, cold cache): the code goes out on this /start, the wrapper decides."""
    h.fake.consent(UID)
    await h.say("/start")  # organic
    await h.say("/start ref_ABCDEF123456")
    assert start_bodies(h)[-1] == {"consent": False, "language": "ru", "referral_code": "ABCDEF123456"}
    assert "Реферальная ссылка принята" in h.last


async def test_ref_param_known_consented_is_one_call(h: Harness):
    h.fake.consent(UID)
    h.ctx.consent.set(UID, True)
    await h.say("/start ref_ABCDEF123456")
    assert start_bodies(h) == [{"consent": False, "language": "ru", "referral_code": "ABCDEF123456"}]


async def test_ref_param_sent_on_every_start_that_carries_one(h: Harness):
    h.fake.consent(UID)
    h.ctx.consent.set(UID, True)
    await h.say("/start ref_ABCDEF123456")
    await h.say("/start ref_ABCDEF123456")
    assert [b.get("referral_code") for b in start_bodies(h)] == ["ABCDEF123456"] * 2


async def test_plain_start_never_sends_a_referral_code(h: Harness):
    h.fake.consent(UID)
    h.ctx.consent.set(UID, True)
    await h.say("/start")
    assert all("referral_code" not in b for b in start_bodies(h))


async def test_invalid_start_param_is_ignored_and_logged(h: Harness, caplog):
    h.fake.consent(UID)
    h.ctx.consent.set(UID, True)
    await h.say("/start ref_bad code!")
    assert all("referral_code" not in b for b in start_bodies(h))
    assert any(r.message == "start_param_invalid" for r in caplog.records)


@pytest.mark.parametrize(
    "code,expect",
    [("SELFSELFSELF", "Свою собственную"), ("NOSUCHCODE", "недействительна")],
)
async def test_referral_rejections_are_told_to_the_user(consented: Harness, code: str, expect: str):
    await consented.say(f"/start ref_{code}")
    assert expect in consented.last


# --- return from payment -------------------------------------------------------------------------


async def test_start_paid_shows_the_payment_status_screen(linked: Harness):
    linked.fake.linked[UID].update(
        tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z"
    )
    await linked.say("/start paid")
    assert "Старт" in linked.last and "21.11.2026" in linked.last
    assert linked.fake.calls("GET", "/me")


async def test_start_paid_while_still_processing_tells_to_check(linked: Harness):
    await linked.say("/start paid")
    assert "Проверить оплату" in linked.last
    assert "Активного тарифа нет" in linked.last


async def test_start_paid_without_consent_asks_consent_first(h: Harness):
    await h.say("/start paid")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]


# --- /stop and blocked ---------------------------------------------------------------------------


async def test_stop_calls_opt_out_and_confirms_in_one_line(consented: Harness):
    await consented.say("/stop")
    assert consented.fake.calls("POST", "/opt-out")
    assert consented.last == texts.stopped()
    assert "\n" not in consented.last


async def test_stop_works_even_without_consent(h: Harness):
    await h.say("/stop")
    assert h.fake.calls("POST", "/opt-out")


async def test_stop_failure_is_not_reported_as_success(consented: Harness):
    consented.fake.queue_error("POST", "/opt-out", 503, "bot_api_disabled")
    await consented.say("/stop")
    assert consented.last != texts.stopped()
    assert consented.last == texts.SERVICE_UNAVAILABLE
    assert consented.ctx.consent.get(UID) is True  # nothing changed


async def test_forbidden_on_a_reply_reports_blocked(h: Harness):
    h.session.errors.append(TelegramForbiddenError(method=SendMessage(chat_id=UID, text="x"), message="Forbidden: bot was blocked by the user"))
    await h.say("/start")
    assert h.fake.calls("POST", "/blocked")[-1]["json"] == {"blocked": True}


async def test_forbidden_when_the_wrapper_call_itself_fails_does_not_loop(h: Harness):
    h.session.errors.append(TelegramForbiddenError(method=SendMessage(chat_id=UID, text="x"), message="Forbidden: user is deactivated"))
    h.fake.queue_error("POST", "/blocked", 500, "boom")
    await h.say("/start")  # must not raise
    assert len(h.fake.calls("POST", "/blocked")) == 1


async def test_non_private_chats_are_ignored(h: Harness):
    await h.say("/start", chat_type="group")
    assert h.fake.requests == [] and h.session.sent() == []
