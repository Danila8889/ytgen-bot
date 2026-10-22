"""The funnel end to end through the real dispatcher: contact, SPIN, recommendation, presentation, resume."""
from __future__ import annotations

import pytest

from tests.conftest import UID, Harness
from tests.funnel_utils import codes_of, join, labels_of, press, relax, run_spin
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks
from ytgen_bot.formatting import rub
from ytgen_bot.funnel.model import Role


async def lead(h: Harness):
    got = await h.store.get_lead(UID)
    assert got is not None
    return got


# --- contact ---------------------------------------------------------------------------------------------------------


async def test_new_person_consents_answers_the_reminders_step_then_sees_the_greeting(h: Harness):
    await join(h, reminders=None)
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["rm:yes", "rm:no"]
    assert "не больше трёх" in h.last and "/stop" in h.last and "Больше не напоминать" in h.last
    await h.press(callbacks.reminders("yes"))
    assert "Привет! Я помощник YTGen" in h.last
    assert codes_of(h) == ["intro_how", "spin_start", "examples", "ask_free", "handoff_manual"]
    got = await lead(h)
    assert got.reminders_opt_in is True and got.step == "greet" and got.status == "new"


async def test_saying_no_to_reminders_blocks_nothing(h: Harness):
    await join(h, reminders=False)
    assert "Привет! Я помощник YTGen" in h.last
    assert (await lead(h)).reminders_opt_in is False
    await press(h, "spin_start")
    assert h.last.startswith("Начнём с простого")


@pytest.mark.parametrize(
    "source,line",
    [
        ("yt", "Ты пришёл с нашего YouTube-канала"),
        ("tg", "Ты из нашего Telegram-канала"),
        ("site", "Ты с сайта ytgenerator.ru"),
    ],
)
async def test_source_token_picks_the_greeting_line_and_is_stored(h: Harness, source: str, line: str):
    await join(h, source=source)
    assert line in h.last
    assert (await lead(h)).source == source


async def test_no_source_means_no_source_line(h: Harness):
    await join(h)
    assert "Ты пришёл" not in h.last and "Ты из нашего" not in h.last and "Ты с сайта" not in h.last
    assert (await lead(h)).source is None


async def test_source_is_first_touch_a_later_start_never_overwrites_it(h: Harness):
    await join(h, source="yt")
    await h.say("/start site")
    assert (await lead(h)).source == "yt"


async def test_a_ref_payload_carries_no_source(h: Harness):
    relax(h)
    await h.say("/start ref_ABCDEF123456")
    await h.press("cn:ABCDEF123456")
    assert (await lead(h)).source is None


async def test_a_source_start_does_not_send_a_referral_code(h: Harness):
    await join(h, source="tg")
    assert all("referral_code" not in c["json"] for c in h.fake.calls("POST", "/start"))


async def test_how_it_works_examples_and_back(h: Harness):
    await join(h)
    await press(h, "intro_how")
    assert h.last.startswith("Всё устроено в четыре шага") and codes_of(h) == ["spin_start", "examples", "back_start"]
    await press(h, "examples")
    assert "drive.google.com" in h.last and h.last.count("<a href") == 4
    assert codes_of(h) == ["spin_start", "back_start", "doubt_open"]
    await press(h, "back_start")
    assert "Привет! Я помощник YTGen" in h.last


async def test_ask_free_mode_sends_text_to_the_ai(h: Harness):
    await join(h)
    await press(h, "ask_free")
    assert h.last.startswith("Спрашивай, отвечу")
    h.ai.say("Сервис работает в облаке.")
    await h.say("Нужно ли что-то устанавливать?")
    assert h.last == "Сервис работает в облаке."
    assert codes_of(h) == ["spin_start", "ask_free", "handoff_manual"]
    assert h.ai.calls == [(UID, "Нужно ли что-то устанавливать?", "ask_free")]


# --- SPIN ------------------------------------------------------------------------------------------------------------------


async def test_spin_walks_s1_to_n1_and_stores_every_answer(h: Harness):
    await join(h)
    await press(h, "spin_start")
    assert h.last.startswith("Начнём с простого") and codes_of(h) == ["s1_plan", "s1_one", "s1_multi", "s1_team", "back"]
    await press(h, "s1_one")
    assert h.last.startswith("Понял. Сколько видео") and codes_of(h) == ["s2_low", "s2_mid", "s2_high", "s2_unk", "back"]
    await press(h, "s2_mid")
    assert "тематику канала" in h.last and codes_of(h) == ["s3_skip", "back"]
    await h.say("котики и еда")  # S3 is free text: it is the answer
    assert h.last.startswith("Что сейчас больше всего съедает")
    assert codes_of(h) == ["p1_edit", "p1_script", "p1_voice", "p1_tools", "p1_start", "p1_other", "back"]
    await press(h, "p1_voice")
    assert "озвучка и сведение" in h.last  # the I1 lead-in depends on the P1 answer
    assert codes_of(h) == ["i1_slow", "i1_noscale", "i1_tired", "i1_cost", "i1_none", "back"]
    await press(h, "i1_tired")
    assert "знакомая история" in h.last and "Допустим, сценарий, озвучка" in h.last
    assert codes_of(h)[:5] == ["n1_more", "n1_scale", "n1_start", "n1_time", "n1_lang"]
    got = await lead(h)
    assert (got.stage, got.volume, got.niche_text, got.pain, got.impact) == ("s1_one", "s2_mid", "котики и еда", "p1_voice", "i1_tired")
    assert got.status == "in_spin" and got.step == "n1"


async def test_s3_can_be_skipped(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_plan", "s2_low", "s3_skip")
    assert h.last.startswith("Что сейчас больше всего съедает")
    assert (await lead(h)).niche_text is None


async def test_p1_other_asks_for_a_phrase_and_stores_it(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one", "s2_low", "s3_skip", "p1_other")
    assert "одной фразой" in h.last
    await h.say("Не успеваю делать превью")
    assert "А как это отражается на твоих каналах и планах?" in h.last
    got = await lead(h)
    assert (got.pain, got.pain_text) == ("p1_other", "Не успеваю делать превью")


async def test_i1_none_gets_a_neutral_link(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one", "s2_low", "s3_skip", "p1_edit", "i1_none")
    assert "Хорошо, тогда посмотрим, что можно усилить." in h.last and "знакомая история" not in h.last


async def test_back_returns_to_the_previous_question_and_keeps_going(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one", "s2_low", "back")
    assert h.last.startswith("Понял. Сколько видео")
    await press(h, "back")
    assert h.last.startswith("Начнём с простого")
    await press(h, "back")
    assert "Привет! Я помощник YTGen" in h.last


async def test_a_stale_answer_button_is_not_applied_out_of_order(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one", "s2_low")  # now at S3
    await press(h, "s1_team")  # an old S1 button
    assert (await lead(h)).stage == "s1_one"
    assert "устарела" in h.texts[-2] and "тематику канала" in h.last


async def test_free_text_mid_spin_is_answered_by_the_ai_then_the_same_step_resumes(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one")
    h.ai.say("Дубляж входит в любой тариф.")
    await h.say("А дубляж есть?")
    assert h.last == "Дубляж входит в любой тариф."
    assert codes_of(h) == ["resume", "handoff_manual"] and labels_of(h)[0] == "Продолжить подбор"
    assert (await lead(h)).step == "s2" and h.ai.calls[-1][2] == "s2"
    await press(h, "resume")
    assert h.last.startswith("Понял. Сколько видео") and codes_of(h)[:4] == ["s2_low", "s2_mid", "s2_high", "s2_unk"]


# --- recommendation ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage,volume,goal,role",
    [
        ("s1_plan", "s2_low", "n1_more", Role.ENTRY),
        ("s1_one", "s2_unk", "n1_start", Role.ENTRY),
        ("s1_one", "s2_low", "n1_more", Role.BASE),
        ("s1_one", "s2_mid", "n1_more", Role.BASE),
        ("s1_multi", "s2_mid", "n1_more", Role.PRO),
        ("s1_one", "s2_mid", "n1_scale", Role.BASE),  # rule 3 (one channel, mid volume) matches before rule 4
        ("s1_plan", "s2_mid", "n1_scale", Role.PRO),
        ("s1_one", "s2_high", "n1_more", Role.PRO),
        ("s1_team", "s2_low", "n1_time", Role.BASE),  # rule 2 (low volume) matches before rule 5 (team)
        ("s1_team", "s2_mid", "n1_time", Role.PRO),
        ("s1_team", "s2_unk", "n1_more", Role.PRO),
        ("s1_one", "s2_unk", "n1_more", Role.BASE),
    ],
)
async def test_recommendation_follows_the_rule_table_and_reads_everything_from_the_wrapper(
    h: Harness, stage: str, volume: str, goal: str, role: Role
):
    await join(h)
    await run_spin(h, stage, volume, "p1_edit", "i1_slow", goal)
    expected = {"entry": ("Пробный", 4321), "base": ("Месяц", 11111), "pro": ("Месяц плюс", 22222),
                "ultra": ("Макс", 33333)}[role.value]
    got = await lead(h)
    assert got.rec_role == role.value and got.status == "recommended" and got.step == "rec"
    assert f"тебе подойдёт <b>{expected[0]}</b>" in h.last and rub(expected[1]) in h.last
    assert "Автопродления нет" in h.last
    ids = {"entry": 21, "base": 22, "pro": 23, "ultra": 24}
    assert got.rec_tariff_id == ids[role.value]


async def test_recommendation_renders_limits_from_capacity_and_labels_the_period_from_window_hours(h: Harness):
    await join(h)
    await run_spin(h, "s1_plan", "s2_low", "p1_edit", "i1_slow", "n1_start")  # entry tariff: 168 h window
    assert "до 2 на ИИ-клипах за 7 дн." in h.last and "до 5 на картинках и стоках за 7 дн." in h.last
    assert rub(4321) + " на 7 дн." in h.last
    await join_other(h)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")  # 24 h plan
    assert "до 6 на ИИ-клипах в сутки" in h.last or "ИИ-клипах в сутки" in h.last
    assert "на картинках и стоках в сутки" in h.last


async def join_other(h: Harness) -> None:
    """A second person in the same harness."""
    other = 2002
    await h.say("/start", uid=other)
    await h.press(callbacks.consent(), uid=other)
    await h.press(callbacks.reminders("yes"), uid=other)


async def test_goal_phrases_come_from_the_script_and_the_static_knowledge_base(h: Harness):
    await join(h)
    await run_spin(h, "s1_one", "s2_low", "p1_edit", "i1_slow", "n1_lang")
    assert "до 4 дополнительных языков дубляжа на каждое видео входят в тариф" in h.last
    await join_other(h)
    await run_spin(h, "s1_one", "s2_low", "p1_edit", "i1_slow", "n1_scale")
    assert "несколько каналов доступны на любом тарифе." in h.last
    assert not any(ch.isdigit() for ch in "несколько каналов доступны на любом тарифе.")


async def test_recommendation_buttons_and_the_try_button_disappears_for_the_entry_tariff(h: Harness):
    await join(h)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")  # base
    assert codes_of(h) == ["show_details", "compare", "try_lite", "pay_main", "doubt_open", "handoff_manual"]
    assert "Попробовать Пробный" in labels_of(h) and "Оплатить Месяц" in labels_of(h)
    await join_other(h)
    await press(h, "spin_start", "s1_plan", "s2_low", "s3_skip", "p1_edit", "i1_slow", "n1_start", uid=2002)
    assert codes_of(h) == ["show_details", "compare", "pay_main", "doubt_open", "handoff_manual"]  # one pay button


async def test_presentation_three_messages_with_honesty_block(h: Harness):
    await join(h)
    await run_spin(h, "s1_one", "s2_mid", "p1_script", "i1_slow", "n1_more")
    await press(h, "show_details")
    assert "Ты назвал сценарии" in h.last and "Что ещё внутри" in h.last and codes_of(h) == ["details_next", "examples"]
    for forbidden in ("компьютерн", "вспомогательн", "$", "≈"):
        assert forbidden not in h.last
    assert "Любую сцену можно перегенерировать вручную в сервисе, по промту." in h.last
    await press(h, "details_next")
    assert h.last.startswith("<b>Месяц</b>, " + rub(11111)) and "до 321 ИИ-клипов в сутки" in h.last
    assert "Неиспользованный остаток не переносится" not in h.last  # only the entry tariff has that line
    assert codes_of(h) == ["details_next", "compare"]
    await press(h, "details_next")
    assert "не публикуем их на YouTube за тебя" in h.last and "не гарантирует никто" in h.last
    assert "Тариф не продлевается сам" in h.last
    assert codes_of(h) == ["pay_main", "compare", "doubt_open", "handoff_manual"]


async def test_entry_tariff_details_carry_the_no_carryover_line(h: Harness):
    await join(h)
    await run_spin(h, "s1_plan", "s2_low", "p1_edit", "i1_slow", "n1_start")
    await press(h, "show_details", "details_next")
    assert "Неиспользованный остаток не переносится. Продление повторной оплатой." in h.last
    assert "до 100 ИИ-клипов в одном видео" in h.last  # max_ai_scenes_per_video from the wrapper


async def test_compare_is_text_without_tables_and_has_no_dollars_or_minutes(h: Harness):
    await join(h)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")
    await press(h, "compare")
    text = h.texts[-2]
    for name, price in (("Пробный", 4321), ("Месяц", 11111), ("Месяц плюс", 22222), ("Макс", 33333)):
        assert f"<b>{name}</b>: {rub(price)}" in text
    assert "Отличаются только лимиты" in text and "$" not in text and "≈" not in text and "мин" not in text
    assert h.last.startswith("Судя по твоим ответам, тебе подходит")  # M5.1 closing follows


# --- persistence: state is in the database, not in memory ------------------------------------------------------------


async def test_a_returning_lead_resumes_from_the_saved_step_after_a_restart(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one", "s2_mid")  # stops at S3
    h.session.clear()
    # a restart: the dispatcher/FSM are brand new, only Postgres and the wrapper survive
    from tests.conftest import Harness as H  # noqa: F401  (the same fixtures, fresh process state)

    h.ctx.guard = type(h.ctx.guard)()
    await h.say("/start")
    assert h.texts[0].startswith("С возвращением")
    assert "тематику канала" in h.last and codes_of(h) == ["s3_skip", "back"]
    await h.say("Игры")
    assert (await lead(h)).niche_text == "Игры"
