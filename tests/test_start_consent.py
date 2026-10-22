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
        ("yt", "source", None),
        ("tg", "source", None),
        ("site", "source", None),
        ("YT", "invalid", None),
        ("yt_", "invalid", None),
        ("site=1", "invalid", None),
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
    assert param.source == (raw if kind == "source" else None)  # a ref_ payload carries no source


# --- consent gate --------------------------------------------------------------------------------


async def test_new_user_sees_consent_with_all_three_links_and_nothing_is_written(h: Harness):
    await h.say("/start")
    text, markup = h.last, h.session.last_markup()
    assert "https://ytgenerator.ru/legal/oferta#section-10" in text
    assert 'href="https://ytgenerator.ru/legal/oferta"' in text
    assert "https://ytgenerator.ru/legal/privacy#section-11" in text
    assert [b.callback_data for b in buttons(markup)] == ["cn"]
    # Only the read-only state call was made: no POST /start, no row, no consent claimed.
    assert [(c["method"], c["path"]) for c in h.fake.requests] == [("GET", "/me")]
    assert h.fake.users == {}


@pytest.mark.parametrize("command", ["/tariffs", "/referral", "/status", "/help", "/me", "random text"])
async def test_nothing_works_before_consent(h: Harness, command: str):
    await h.say(command)
    assert "Согласен" in [b.text for b in buttons(h.session.last_markup())]
    # Only the consent read ran: no tariffs / referral / billing call, and no POST /start probe.
    assert {(c["method"], c["path"]) for c in h.fake.requests} == {("GET", "/me")}
    assert h.fake.users == {}


async def test_callbacks_are_gated_too(h: Harness):
    await h.press(callbacks.menu("tariffs"))
    await h.press(callbacks.tariff(11))
    await h.press(callbacks.check("pay-1"))
    assert {(c["method"], c["path"]) for c in h.fake.requests} == {("GET", "/me")}
    assert texts.CONSENT_FIRST in [a.text for a in h.session.answers()]


async def test_consent_button_sends_consent_true_then_the_funnel_starts(h: Harness):
    await h.say("/start")
    await h.press(callbacks.consent())
    assert start_bodies(h) == [{"consent": True, "language": "ru"}]
    assert h.fake.user(UID)["consent_at"] is not None
    assert "Сам сервис в Telegram не используется" in h.texts[-2]
    # an unlinked person enters the sales funnel: the (separate) reminders opt-in comes first
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["rm:yes", "rm:no"]
    # Gate is open now.
    await h.say("/tariffs")
    assert "Старт" in h.last and rub(1234) in h.last


async def test_double_tap_on_consent_calls_once(h: Harness):
    await h.press(callbacks.consent())
    await h.press(callbacks.consent())
    assert [b for b in start_bodies(h) if b.get("consent")] == [{"consent": True, "language": "ru"}]
    assert texts.ALREADY_PROCESSING in [a.text for a in h.session.answers()]


async def test_server_side_consent_is_honoured_without_asking_again(h: Harness):
    """A fresh process knows nothing; consent stored in the wrapper is read from /me, never re-asked."""
    h.fake.consent(UID)
    await h.say("/tariffs")
    assert "Старт" in h.last
    assert not h.fake.calls("POST", "/start")  # no probe, no side effect
    assert h.fake.calls("GET", "/me")


async def test_consent_is_read_on_every_update_so_an_opt_out_applies_at_once(consented: Harness):
    h = consented
    await h.say("/help")
    assert "YouTubeGen Bot" in h.last
    h.fake.user(UID)["opted_out_at"] = "2026-10-22T11:00:00Z"  # e.g. /stop on another instance
    await h.say("/help")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]


async def test_opt_out_requires_consent_again(consented: Harness):
    h = consented
    await h.say("/stop")
    assert h.fake.user(UID)["opted_out_at"] is not None
    await h.say("/tariffs")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]
    await h.press(callbacks.consent())
    assert h.fake.user(UID)["opted_out_at"] is None
    await h.say("/tariffs")
    assert "Старт" in h.last


async def test_consent_gate_failure_is_shown_not_skipped(h: Harness):
    h.fake.queue_error("GET", "/me", 503, "bot_api_disabled")
    await h.say("/tariffs")
    assert h.last == texts.SERVICE_UNAVAILABLE
    assert not h.fake.calls("GET", "/tariffs")


# --- referral code parsing -----------------------------------------------------------------------


async def test_ref_param_is_held_in_the_consent_button_and_sent_only_after_it(h: Harness):
    await h.say("/start ref_ABCDEF123456")
    assert h.fake.calls("POST", "/start") == []  # nothing, in particular no code, before consent
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn:ABCDEF123456"]
    await h.press("cn:ABCDEF123456")
    assert start_bodies(h) == [{"consent": True, "language": "ru", "referral_code": "ABCDEF123456"}]
    assert "Реферальная ссылка принята" in h.texts[-2]


async def test_ref_param_for_an_already_started_user_is_still_sent(consented: Harness):
    """Organic start earlier (consented): the code goes out on this /start, the wrapper decides."""
    h = consented
    await h.say("/start")  # organic
    await h.say("/start ref_ABCDEF123456")
    assert start_bodies(h)[-1] == {"consent": False, "language": "ru", "referral_code": "ABCDEF123456"}
    assert "Реферальная ссылка принята" in h.texts[-1]  # shown together with the one-time funnel consent


async def test_ref_param_is_one_start_call(consented: Harness):
    h = consented
    await h.say("/start ref_ABCDEF123456")
    assert start_bodies(h) == [{"consent": False, "language": "ru", "referral_code": "ABCDEF123456"}]


async def test_ref_param_sent_on_every_start_that_carries_one(consented: Harness):
    h = consented
    await h.say("/start ref_ABCDEF123456")
    await h.say("/start ref_ABCDEF123456")
    assert [b.get("referral_code") for b in start_bodies(h)] == ["ABCDEF123456"] * 2


async def test_plain_start_never_sends_a_referral_code(consented: Harness):
    h = consented
    await h.say("/start")
    assert all("referral_code" not in b for b in start_bodies(h))


async def test_invalid_start_param_is_ignored_and_logged(consented: Harness, caplog):
    h = consented
    await h.say("/start ref_bad code!")
    assert all("referral_code" not in b for b in start_bodies(h))
    assert any(r.message == "start_param_invalid" for r in caplog.records)


@pytest.mark.parametrize(
    "code,expect",
    [("SELFSELFSELF", "Свою собственную"), ("NOSUCHCODE", "недействительна")],
)
async def test_referral_rejections_are_told_to_the_user(consented: Harness, code: str, expect: str):
    await consented.say(f"/start ref_{code}")
    assert expect in consented.texts[-1]


# --- return from payment (GET /billing/payments/latest) -------------------------------------------


async def test_start_paid_shows_the_real_result_of_the_latest_payment(linked: Harness):
    h = linked
    h.ctx.watchers = None
    await h.press(callbacks.pay(11, 1234))
    pid = h.fake.latest[UID]
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z")
    await h.say("/start paid")
    assert h.fake.calls("GET", "/billing/payments/latest")
    assert "Оплата прошла" in h.last and "Старт" in h.last and "21.11.2026" in h.last


async def test_start_paid_cancelled_payment(linked: Harness):
    h = linked
    h.ctx.watchers = None
    await h.press(callbacks.pay(11, 1234))
    h.fake.payments[h.fake.latest[UID]].update(status="canceled", reason="insufficient_funds")
    await h.say("/start paid")
    assert "Платёж не прошёл" in h.last and "Недостаточно средств" in h.last


async def test_start_paid_while_still_pending_offers_the_check_button_and_watches(linked: Harness):
    h = linked
    await h.press(callbacks.pay(11, 1234))
    pid = h.fake.latest[UID]
    h.ctx.watchers._tasks.clear()
    await h.say("/start paid")
    assert h.last == texts.payment_still_pending()
    assert buttons(h.session.last_markup())[0].callback_data == callbacks.check(pid)
    assert h.ctx.watchers.active(pid)


async def test_start_paid_paid_but_not_credited_yet(linked: Harness):
    h = linked
    h.ctx.watchers = None
    await h.press(callbacks.pay(11, 1234))
    h.fake.payments[h.fake.latest[UID]].update(status="succeeded", credited=False)
    await h.say("/start paid")
    assert h.last == texts.payment_credit_delayed()


async def test_start_paid_falls_back_to_account_state_only_when_there_is_no_payment(linked: Harness):
    h = linked
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at="2026-11-21T10:00:00Z")
    await h.say("/start paid")  # 404 payment_not_found
    assert "не найден" in h.last and "Старт" in h.last and "21.11.2026" in h.last


async def test_start_paid_unlinked_falls_back_to_the_unlinked_state(consented: Harness):
    await consented.say("/start paid")  # 403 telegram_not_linked
    assert "не найден" in consented.last and texts.status_unlinked() in consented.last


async def test_start_paid_other_errors_are_shown_not_swallowed(linked: Harness):
    linked.fake.queue_error("GET", "/billing/payments/latest", 502, "payment_provider_unknown_status")
    await linked.say("/start paid")
    assert "неожиданный статус" in linked.last


async def test_start_paid_without_consent_asks_consent_first(h: Harness):
    await h.say("/start paid")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]
    assert not h.fake.calls("GET", "/billing/payments/latest")


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
    assert consented.fake.user(UID)["opted_out_at"] is None  # nothing changed


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
