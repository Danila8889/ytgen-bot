"""Objection handling (the script speaks, the AI only labels), hand-off to the manager, and the free-text triggers."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from tests.conftest import MANAGER, SUPPORT, UID, Harness
from tests.funnel_utils import codes_of, join, labels_of, press, run_spin
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks
from ytgen_bot.formatting import rub


async def lead(h: Harness):
    got = await h.store.get_lead(UID)
    assert got is not None
    return got


async def at_recommendation(h: Harness, **join_kw) -> Harness:
    await join(h, **join_kw)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")
    return h


def manager_cards(h: Harness) -> list[str]:
    return h.session.texts(MANAGER)


# --- objections ---------------------------------------------------------------------------------------------------------------


async def test_doubt_opens_the_objection_menu(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open")
    assert h.last == "Хорошо, что спрашиваешь. Что именно смущает?"
    assert codes_of(h) == ["obj_price", "obj_quality", "obj_hard", "obj_ban", "obj_gpt", "obj_time", "obj_roi",
                           "obj_refund", "obj_think"]


async def test_price_objection_quotes_the_entry_tariff_from_the_wrapper(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_price")
    assert "<b>Пробный</b>" in h.last and rub(4321) + " на 7 дн." in h.last
    assert "до 2 на ИИ-клипах за 7 дн." in h.last and "платишь только за 7 дн." in h.last
    assert codes_of(h) == ["try_lite", "compare", "handoff_manual"] and labels_of(h)[0] == "Попробовать Пробный"
    assert await h.store.objection_codes(UID) == ["obj_price"]


async def test_price_objection_without_tariffs_degrades_to_a_number_free_text_and_logs_error(h: Harness, caplog):
    await at_recommendation(h)
    h.fake.queue_error("GET", "/v1/public/tariffs", 500, "boom")
    h.ctx.funnel.tariffs.invalidate()
    await press(h, "doubt_open", "obj_price")
    assert "/tariffs" in h.last and "₽" not in h.last
    assert any(r.levelno == logging.ERROR and r.getMessage() == "funnel_tariffs_unavailable" for r in caplog.records)
    assert labels_of(h)[0] == "Попробовать недорогой тариф"


async def test_quality_objection_branches_have_no_computer_vision_claim(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_quality")
    assert codes_of(h) == ["q_visual", "q_voice", "q_template"]
    await press(h, "q_visual")
    assert "Veo 3.1" in h.last and "Любую сцену можно перегенерировать вручную в сервисе, по промту." in h.last
    for forbidden in ("Компьютерное зрение", "компьютерное зрение", "автоматически перегенер", "проверяет кадры"):
        assert forbidden not in h.last
    assert h.last.endswith("Лучше один раз увидеть.") and codes_of(h) == ["examples", "try_lite"]
    await press(h, "q_voice")
    assert "ElevenLabs" in h.last
    await press(h, "q_template")
    assert "не собирает видео по одному шаблону" in h.last


async def test_hard_objection_branches(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_hard", "h_setup")
    assert "конструкторе" in h.last and codes_of(h) == ["try_lite", "intro_how", "handoff_manual"]
    await press(h, "h_yt")
    assert "закрытая Telegram-группа" in h.last


async def test_ban_answer_is_honest_and_drops_the_auxiliary_scenes_claim(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_ban")
    text = h.last
    assert "гарантировать монетизацию и отсутствие ограничений со стороны YouTube не может никто" in text
    assert "вспомогательн" not in text
    for forbidden in ("защищено", "не демонетизируют", "безопасно для YouTube"):
        assert forbidden not in text
    urls = [b.url for b in buttons(h.session.last_markup()) if b.url]
    assert urls == ["https://ytgenerator.ru/legal/oferta"] and codes_of(h) == ["try_lite", "handoff_manual"]


async def test_gpt_objection_flow_to_the_presentation_or_an_honest_exit(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_gpt")
    assert codes_of(h) == ["g_script", "g_video"]
    await press(h, "g_script")
    assert "ChatGPT даёт текст" in h.last and codes_of(h) == ["g_all", "g_text"]
    await press(h, "g_text")
    assert "Тогда сервис, возможно, лишний" in h.last
    await press(h, "obj_gpt", "g_video", "g_all")
    assert "Ты назвал сборку роликов" in h.last  # stage 3 (the presentation)


async def test_roi_and_refund_and_time_and_think(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_roi")
    assert "не скажет никто" in h.last and rub(4321) in h.last
    await press(h, "obj_refund")
    assert "в разделе 4 оферты" in h.last and "Автопродления нет" in h.last
    assert "возврат без вопросов" not in h.last
    await press(h, "obj_time")
    assert "менеджер подскажет" in h.last
    await press(h, "obj_think")
    assert codes_of(h) == ["examples", "remind_ok", "handoff_manual", "end_polite"]


async def test_the_same_objection_a_second_time_offers_a_human_instead_of_repeating(h: Harness):
    await at_recommendation(h)
    await press(h, "doubt_open", "obj_price")
    first = h.last
    await press(h, "doubt_open", "obj_price")
    assert h.last != first and "Повторяться не буду" in h.last and codes_of(h) == ["handoff_manual"]
    assert await h.store.objection_codes(UID) == ["obj_price", "obj_price"]  # kept on record
    await press(h, "handoff_manual")
    got = await lead(h)
    assert got.status == "handoff" and got.handoff_reason == "repeat_objection"


async def test_free_text_objection_is_classified_by_the_ai_but_the_script_speaks(h: Harness):
    await at_recommendation(h)
    h.ai.say("Любой текст модели про цену, который не должен быть показан.", objection_code="obj_price")
    await h.say("Это слишком дорого для меня")
    assert "Любой текст модели" not in " ".join(h.texts)
    assert "Понимаю, цена должна соответствовать задаче" in h.last and "<b>Пробный</b>" in h.last
    assert await h.store.objection_codes(UID) == ["obj_price"]


async def test_remind_ok_with_an_opt_in_is_saved_and_without_one_asks_for_it(h: Harness):
    await at_recommendation(h)
    await press(h, "remind_ok")
    assert "Сохранил твой подбор" in h.last
    got = await lead(h)
    assert got.remind_ok is True and got.reminders_opt_in is True


async def test_remind_ok_does_not_replace_the_separate_reminders_opt_in(h: Harness):
    await at_recommendation(h, reminders=False)
    await press(h, "remind_ok")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["rm:yes", "rm:no"]
    assert (await lead(h)).reminders_opt_in is False
    await h.press(callbacks.reminders("yes"))
    got = await lead(h)
    assert got.reminders_opt_in is True and got.remind_ok is True and got.step == "rec"


async def test_end_polite_closes_the_dialogue_as_cold_and_start_resumes(h: Harness):
    await at_recommendation(h)
    await press(h, "end_polite")
    assert (await lead(h)).status == "cold" and "Если захочешь вернуться" in h.last
    await h.say("/start")
    assert (await lead(h)).status == "recommended"
    assert h.texts[-2].startswith("С возвращением")


# --- hand-off --------------------------------------------------------------------------------------------------------------------


async def test_manual_handoff_sends_the_card_and_stops_selling(h: Harness):
    h.fake.tariffs  # noqa: B018 - the fixture default is replaced by join()
    await join(h, source="yt")
    await h.say("первое сообщение")
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_cost", "n1_scale")
    await press(h, "doubt_open", "obj_price")
    await h.say("Хочу понять про дубляж", username="vasya", first_name="Вася")
    await h.press(callbacks.funnel("handoff_manual"), username="vasya", first_name="Вася")
    card = manager_cards(h)[-1]
    assert "Передача менеджеру" in card and "приоритет: <b>обычный</b>" in card
    assert "Источник: yt" in card and f"tg_id <code>{UID}</code>" in card
    assert "stage: s1_one" in card and "volume: s2_mid" in card and "impact: i1_cost" in card and "goal: n1_scale" in card
    assert "Рекомендованный тариф: base (tariff id 22)" in card and "Показанные возражения: obj_price" in card
    assert "Хочу понять про дубляж" in card and "человек просит связаться" in card
    got = await lead(h)
    assert got.status == "handoff" and got.handoff_reason == "manual_request" and got.handoff_at is not None
    assert got.handoff_priority == "normal"
    # the person is told, with SUPPORT_CONTACT (never a hardcoded handle) and a t.me button built from it
    assert "Передал твой запрос менеджеру" in h.last and SUPPORT in h.last and "togn15" not in h.last
    assert [b.url for b in buttons(h.session.last_markup())] == ["https://t.me/ytgen_support_test"]


async def test_the_card_carries_name_username_and_only_the_last_five_messages(h: Harness):
    await join(h)
    for i in range(8):
        await h.say(f"сообщение {i}", username="vasya", first_name="Вася")
    await h.press(callbacks.funnel("handoff_manual"), username="vasya", first_name="Вася")
    card = manager_cards(h)[-1]
    assert "Вася, @vasya" in card
    assert "сообщение 7" in card and "сообщение 3" in card and "сообщение 2" not in card


async def test_the_card_escapes_html_from_the_person(h: Harness):
    await join(h)
    await h.say("<b>x</b> & <script>")
    await press(h, "handoff_manual")
    card = manager_cards(h)[-1]
    assert "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;" in card and "<script>" not in card


async def test_handoff_is_idempotent_one_card_only(h: Harness):
    await join(h)
    await press(h, "handoff_manual", "handoff_manual")
    assert len(manager_cards(h)) == 1
    assert "Запрос уже у менеджера" in h.last


async def test_card_send_failure_is_an_error_log_and_the_person_is_sent_to_support_directly(h: Harness, caplog):
    await join(h)
    h.session.errors.append(TelegramNetworkError(method=SendMessage(chat_id=MANAGER, text="x"), message="down"))
    await press(h, "handoff_manual")
    assert any(r.levelno == logging.ERROR and r.getMessage() == "handoff_card_failed" for r in caplog.records)
    assert "Не получилось передать запрос менеджеру автоматически" in h.last and SUPPORT in h.last
    assert (await lead(h)).status != "handoff"  # nothing pretends it worked; they can retry


async def test_after_handoff_sales_and_reminders_stop_but_simple_questions_are_answered(h: Harness):
    await at_recommendation(h)
    await press(h, "handoff_manual")
    h.session.clear()
    await press(h, "doubt_open", "obj_price")
    assert "Твой запрос уже передан менеджеру" in h.last  # no objection script is played
    h.ai.say("Дубляж входит в любой тариф.")
    await h.say("А дубляж входит?")
    assert h.last == "Дубляж входит в любой тариф."
    assert await h.store.pending_followup(UID) is None


async def test_ai_handoff_flag_shows_the_reply_then_hands_off(h: Harness):
    await join(h)
    h.ai.say("Точно не знаю, лучше уточнить у менеджера.", handoff=True, reason="no fact")
    await h.say("Какой у вас юридический адрес?")
    assert h.session.texts(UID)[-2] == "Точно не знаю, лучше уточнить у менеджера."
    assert (await lead(h)).handoff_reason == "ai_handoff" and len(manager_cards(h)) == 1


# --- deterministic triggers (before the AI, so they work with the AI down) ----------------------------------------------------------


async def test_other_payment_methods_go_to_the_manager_without_calling_the_ai(h: Harness):
    await at_recommendation(h)
    await h.say("Можно оплатить USDT или Visa?")
    assert h.ai.calls == []
    assert (await lead(h)).handoff_reason == "payment_method"
    assert "другой способ оплаты (оплата)" in manager_cards(h)[-1]


async def test_discount_question_gets_the_promo_note_and_never_a_discount(h: Harness):
    await at_recommendation(h)
    await h.say("А скидка будет? Есть промокод?")
    assert h.ai.calls == []
    assert SUPPORT in h.last and "Ввести промокод" in h.last and "%" not in h.last
    assert codes_of(h) == ["pay_main", "handoff_manual"]
    await press(h, "handoff_manual")
    assert (await lead(h)).handoff_reason == "discount"


async def test_refund_dispute_and_complaint_are_high_priority(h: Harness):
    await at_recommendation(h)
    await h.say("Верните деньги, меня списали дважды!")
    got = await lead(h)
    assert got.handoff_reason == "refund_dispute" and got.handoff_priority == "high"
    assert "приоритет: <b>ВЫСОКИЙ</b>" in manager_cards(h)[-1]


async def test_complaint_is_high_priority_and_the_ai_does_not_argue(h: Harness):
    await join(h)
    await h.say("Это обман и развод!")
    assert h.ai.calls == [] and (await lead(h)).handoff_priority == "high"


# --- AI fallbacks ----------------------------------------------------------------------------------------------------------------------------


async def test_ai_failure_shows_the_fallback_text_with_the_handoff_button(h: Harness):
    await join(h)
    await press(h, "spin_start")
    h.ai.fail("failed")
    await h.say("Как это работает?")
    assert h.last == f"Сейчас не могу ответить, напиши менеджеру {SUPPORT}."
    assert codes_of(h) == ["resume", "handoff_manual"]


async def test_validator_blocked_reply_is_replaced_by_the_manager_text(h: Harness):
    await join(h)
    h.ai.fail("blocked")
    await h.say("Гарантируете ли монетизацию?")
    assert h.last == "Тут лучше уточнить у менеджера." and "handoff_manual" in codes_of(h)


async def test_rate_limited_and_ceiling_replies(h: Harness):
    await join(h)
    h.ai.fail("rate_limited")
    await h.say("ещё вопрос")
    assert "Сейчас много вопросов подряд" in h.last
    h.ai.fail("ceiling")
    await h.say("и ещё")
    assert h.last == f"Сейчас не могу ответить, напиши менеджеру {SUPPORT}."


async def test_old_buttons_after_a_handoff_never_restart_sales_or_reminders(h: Harness):
    await at_recommendation(h)
    await press(h, "handoff_manual")
    await press(h, "spin_start", "s1_one", "s2_mid", "s3_skip", "p1_edit", "i1_slow", "n1_more", "pay_main", "go_pay")
    got = await lead(h)
    assert got.status == "handoff"
    assert await h.store.pending_followup(UID) is None


async def test_a_funnel_button_closes_an_open_text_prompt_instead_of_the_next_text_being_eaten_by_it(h: Harness):
    await at_recommendation(h)
    h.fake.link(UID)
    await press(h, "pay_main", "go_pay")
    await h.press(callbacks.promo(22))  # "enter a promo code": the bot now waits for text
    await press(h, "doubt_open")
    h.ai.say("Ответ.")
    await h.say("привет, а что с оплатой?")
    assert h.last == "Ответ." and not h.fake.calls("POST", "/billing/quote")
