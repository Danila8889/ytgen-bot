"""The payment bridge: funnel pay buttons enter the EXISTING in-bot checkout; `paid` comes from the wrapper; M5.6 once;
the manager's /paid; the M5.3 check."""
from __future__ import annotations

import datetime as dt
import logging

from tests.conftest import MANAGER, UID, Harness, make_settings
from tests.funnel_utils import codes_of, join, labels_of, press, run_spin
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks
from ytgen_bot.formatting import rub
from ytgen_bot.funnel import reminders

EXPIRES = "2026-11-21T10:00:00Z"


async def lead(h: Harness, uid: int = UID):
    got = await h.store.get_lead(uid)
    assert got is not None
    return got


async def at_recommendation(h: Harness, **kw) -> Harness:
    await join(h, **kw)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")  # -> base (tariff id 22)
    return h


def pay_ok(h: Harness, tariff_id: int = 22, name: str = "Месяц") -> str:
    pid = h.fake.latest[UID]
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=tariff_id, tariff_name=name, tariff_expires_at=EXPIRES)
    return pid


# --- the pay buttons enter the in-bot checkout -----------------------------------------------------------------------------------------


async def test_pay_main_explains_the_in_bot_payment_and_does_not_send_people_to_the_website(h: Harness):
    await at_recommendation(h)
    await press(h, "pay_main")
    assert "Оплата <b>Месяц</b> проходит прямо здесь, в боте" in h.last
    assert "ytgenerator.ru/auth" not in h.last and "@togn15" not in h.last
    assert codes_of(h) == ["go_pay", "handoff_manual", "back_close"]
    assert [b.url for b in buttons(h.session.last_markup()) if b.url] == []
    assert (await lead(h)).step == "pay:base"


async def test_try_lite_targets_the_entry_tariff_whatever_was_recommended(h: Harness):
    await at_recommendation(h)
    await press(h, "try_lite")
    assert "Оплата <b>Пробный</b> проходит прямо здесь" in h.last
    await press(h, "go_pay")
    assert any(c["json"] is None for c in h.fake.requests)  # sanity: wrapper was reached
    assert h.last.startswith("Для оплаты нужно привязать аккаунт")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(21)]


async def test_go_pay_unlinked_starts_the_link_flow_with_the_recommended_tariff_and_sets_link_sent(h: Harness):
    await at_recommendation(h)
    await press(h, "pay_main", "go_pay")
    got = await lead(h)
    assert got.status == "link_sent"
    assert h.last.startswith("Для оплаты нужно привязать аккаунт")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.link(22)]
    assert await h.store.event_count(UID, "link_sent") == 1


async def test_go_pay_linked_shows_the_tariff_with_the_promo_step_then_the_existing_quote_and_checkout(h: Harness):
    await at_recommendation(h)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    assert h.last.startswith("<b>Месяц</b>") and "Есть промокод?" in h.last
    assert [b.callback_data for b in buttons(h.session.last_markup())] == [callbacks.promo(22), callbacks.quote(22)]
    await h.press(callbacks.quote(22))
    assert f"К оплате: <b>{rub(11111)}</b>" in h.last
    await h.press(callbacks.pay(22, 11111))
    assert h.fake.calls("POST", "/billing/checkout")[-1]["json"]["tariff_id"] == 22
    assert "Платёж создан" in h.texts[-2] or "Платёж создан" in h.last


async def test_the_promo_field_is_the_one_in_the_checkout_and_the_funnel_never_names_a_discount(h: Harness):
    await at_recommendation(h)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    await h.press(callbacks.promo(22))
    await h.say("save10xx")
    assert "К оплате" in h.last and "−" in h.last  # the discount comes from the wrapper's quote, not from the funnel


async def test_back_close_returns_to_the_closing_message(h: Harness):
    await at_recommendation(h)
    await press(h, "pay_main", "back_close")
    assert h.last.startswith("Судя по твоим ответам, тебе подходит")


async def test_pay_buttons_degrade_to_the_manager_when_tariffs_cannot_be_read(h: Harness, caplog):
    await at_recommendation(h)
    h.fake.queue_error("GET", "/v1/public/tariffs", 500, "boom")
    h.ctx.funnel.tariffs.invalidate()
    await press(h, "pay_main")
    assert "Сейчас я не могу показать тариф для оплаты" in h.last and "@ytgen_support_test" in h.last
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# --- paid comes from the wrapper -----------------------------------------------------------------------------------------------------------


async def test_a_successful_payment_marks_the_lead_paid_and_sends_m56_exactly_once(h: Harness):
    await at_recommendation(h)
    h.ctx.watchers = None
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    await h.press(callbacks.pay(22, 11111))
    pid = pay_ok(h)
    await h.press(callbacks.check(pid))
    assert "Оплата прошла" in h.texts[-2]
    assert h.last.startswith("Отлично, добро пожаловать!")
    assert "ytgen_support_test" in h.last and "Менеджер добавит" not in h.last  # access is "clarified with the manager"
    got = await lead(h)
    assert got.status == "paid" and got.paid_tariff == "Месяц" and got.paid_at is not None and got.paid_by_manager is False
    before = len(h.session.texts(UID))
    await h.press(callbacks.check(pid))  # the same result again: no second announcement
    assert sum("добро пожаловать" in t for t in h.session.texts(UID)) == 1
    assert len(h.session.texts(UID)) >= before


async def test_paid_stops_reminders_and_sales(h: Harness):
    await at_recommendation(h)
    h.ctx.watchers = None
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    await h.press(callbacks.pay(22, 11111))
    assert await h.store.pending_followup(UID) is not None  # the M5.3 check is scheduled
    await h.press(callbacks.check(pay_ok(h)))
    assert await h.store.pending_followup(UID) is None
    await press(h, "doubt_open")
    assert "Оплата уже подтверждена" in h.last


async def test_start_of_a_person_who_already_has_paid_access_skips_the_funnel(h: Harness):
    h.fake.consent(UID)
    h.fake.link(UID, tariff_id=22, tariff_name="Месяц", tariff_expires_at=EXPIRES)
    await h.say("/start")
    assert "Выберите действие" in h.last and "Тарифы и оплата" in [b.text for b in buttons(h.session.last_markup())]
    assert await h.store.get_lead(UID) is None  # no funnel row is created for a customer


async def test_a_lead_who_paid_elsewhere_is_marked_paid_at_start_from_the_wrapper_not_from_their_word(h: Harness):
    await at_recommendation(h)
    await h.say("я уже оплатил")  # their word: nothing changes
    assert (await lead(h)).status != "paid"
    h.fake.link(UID, tariff_id=22, tariff_name="Месяц", tariff_expires_at=EXPIRES)
    await h.say("/start")
    assert (await lead(h)).status == "paid"
    assert h.session.texts(UID)[-2].startswith("Отлично, добро пожаловать!")
    await h.say("/start")
    assert sum("добро пожаловать" in t for t in h.session.texts(UID)) == 1


# --- M5.3: "did the payment work?" --------------------------------------------------------------------------------------------------------


async def test_m53_is_scheduled_thirty_minutes_after_go_pay_and_sent_once(h: Harness):
    await at_recommendation(h)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    pending = await h.store.pending_followup(UID)
    assert pending is not None and pending.kind == "m53"
    assert pending.due_at == (await lead(h)).last_activity_at + dt.timedelta(minutes=30)
    loop = reminders.ReminderLoop(h.ctx.funnel, h.ctx, h.bot, sleep=_nosleep)
    assert await loop.tick() == 0  # not due yet
    h.clock.advance(minutes=31)
    assert await loop.tick() == 1
    assert h.last == "Получилось оплатить и войти?"
    assert codes_of(h) == ["pay_done", "handoff_manual", "pay_not_yet"]
    h.clock.advance(hours=5)
    assert await loop.tick() == 0  # once


async def _nosleep(_: float) -> None:
    return None


async def test_m53_is_not_sent_without_the_reminders_opt_in(h: Harness):
    await at_recommendation(h, reminders=False)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    assert await h.store.pending_followup(UID) is None


async def test_m53_when_the_wrapper_already_says_paid_sends_the_welcome_instead_of_the_question(h: Harness):
    await at_recommendation(h)
    h.ctx.watchers = None
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    await h.press(callbacks.pay(22, 11111))
    pay_ok(h)
    h.clock.advance(minutes=31)
    loop = reminders.ReminderLoop(h.ctx.funnel, h.ctx, h.bot, sleep=_nosleep)
    await loop.tick()
    assert h.last.startswith("Отлично, добро пожаловать!") and (await lead(h)).status == "paid"
    assert not any(t == "Получилось оплатить и войти?" for t in h.texts)


async def test_pay_done_is_checked_against_the_wrapper_not_the_persons_word(h: Harness):
    await at_recommendation(h)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay", "pay_done")
    assert "Пока не вижу активного тарифа" in h.last and codes_of(h) == ["pay_done", "handoff_manual"]
    assert (await lead(h)).status == "link_sent"
    h.fake.linked[UID].update(tariff_id=22, tariff_name="Месяц", tariff_expires_at=EXPIRES)
    await press(h, "pay_done")
    assert h.last.startswith("Отлично, добро пожаловать!") and (await lead(h)).status == "paid"


async def test_pay_not_yet_goes_back_to_recommended_and_writes_no_more(h: Harness):
    await at_recommendation(h)
    await press(h, "pay_main", "go_pay", "pay_not_yet")
    assert (await lead(h)).status == "recommended" and "Хорошо, не буду тревожить" in h.last


# --- the manager's /paid -------------------------------------------------------------------------------------------------------------------------


async def test_manager_paid_marks_a_lead_paid_with_a_note_and_welcomes_them_once(h: Harness):
    await at_recommendation(h)
    await h.say("/paid 1001 оплатил по счёту", uid=MANAGER)  # the manager chat is a private chat with id MANAGER
    assert h.session.texts(MANAGER)[-1] == "Готово: 1001 отмечен как оплативший."
    got = await lead(h)
    assert got.status == "paid" and got.paid_by_manager is True and got.notes == "оплатил по счёту"
    assert h.session.texts(UID)[-1].startswith("Отлично, добро пожаловать!")
    await h.say("/paid 1001", uid=MANAGER)
    assert h.session.texts(MANAGER)[-1] == "Лид 1001 уже отмечен как оплативший."
    assert sum("добро пожаловать" in t for t in h.session.texts(UID)) == 1


async def test_manager_paid_in_a_group_chat_is_served_only_there(h: Harness):
    group = -1001234567890
    h.ctx.settings = make_settings(manager_chat_id=group)
    h.ctx.funnel.manager_chat_id = group
    await at_recommendation(h)
    await h.say("/paid 1001 нал", uid=555, chat_type="supergroup", chat_id=group)
    assert (await lead(h)).status == "paid"
    assert h.session.texts(group)[-1] == "Готово: 1001 отмечен как оплативший."
    # any OTHER group is ignored by the guard before it reaches a handler
    h.session.clear()
    await h.say("/paid 1001", uid=555, chat_type="supergroup", chat_id=-100999)
    assert h.session.sent() == []


async def test_manager_paid_from_anyone_else_is_ignored_silently_with_an_info_log_fail_closed(h: Harness, caplog):
    await at_recommendation(h)
    h.session.clear()
    await h.say("/paid 1001 trust me", uid=424242)
    assert h.session.sent() == []  # no reply at all
    assert (await lead(h)).status != "paid"
    assert any(r.levelno == logging.INFO and r.getMessage() == "paid_command_denied" for r in caplog.records)


async def test_manager_paid_bad_usage_and_unknown_lead(h: Harness):
    await h.say("/paid", uid=MANAGER)
    assert h.session.texts(MANAGER)[-1] == "Формат: /paid <tg_id> [заметка]"
    await h.say("/paid abc", uid=MANAGER)
    assert h.session.texts(MANAGER)[-1] == "Формат: /paid <tg_id> [заметка]"
    await h.say("/paid 31337", uid=MANAGER)
    assert h.session.texts(MANAGER)[-1] == "Лид 31337 не найден."


async def test_help_with_payment_is_a_handoff_marked_as_payment(h: Harness):
    await at_recommendation(h)
    await press(h, "pay_main", "handoff_manual")
    assert (await lead(h)).handoff_reason == "payment_help"
    assert "помощь с оплатой" in h.session.texts(MANAGER)[-1]
    assert labels_of(h) == ["Написать @ytgen_support_test"]  # the manager link button is built from SUPPORT_CONTACT
