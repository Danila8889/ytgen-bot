"""Consent comes first: the new consent text and links, funnel data only after consent, the separate reminders opt-in."""
from __future__ import annotations

import pytest

from tests.conftest import UID, Harness
from tests.funnel_utils import join, press, relax
from tests.mocked_telegram import buttons
from ytgen_bot import callbacks, texts


async def test_the_consent_text_links_every_new_privacy_anchor_and_stays_factual(h: Harness):
    await h.say("/start")
    text = h.last
    base = "https://ytgenerator.ru/legal/privacy"
    assert f'href="{base}#section-11"' in text
    for clause in range(9, 17):
        assert f'href="{base}#clause-11-{clause}"' in text, clause
    assert "https://ytgenerator.ru/legal/oferta#section-10" in text and 'href="https://ytgenerator.ru/legal/oferta"' in text
    assert "Нажимая «Согласен»" in text and "/stop" in text
    assert "kie.ai" in text  # the AI assistant processing is named
    assert text == texts.consent_request()


async def test_before_consent_the_funnel_does_nothing_and_stores_nothing(h: Harness):
    relax(h)
    await h.say("/start yt")
    await h.say("привет, хочу узнать про цены")
    await h.press(callbacks.funnel("spin_start"))
    await h.press(callbacks.reminders("yes"))
    assert await h.store.get_lead(UID) is None
    assert await h.store.user_row_counts(UID) == {k: 0 for k in await h.store.user_row_counts(UID)}
    assert h.ai.calls == []
    assert {(c["method"], c["path"]) for c in h.fake.requests} == {("GET", "/me")}


async def test_stop_and_the_consent_button_are_the_only_things_that_work_before_consent(h: Harness):
    await h.say("/stop")
    assert h.fake.calls("POST", "/opt-out")
    await h.press(callbacks.consent())
    assert h.fake.user(UID)["consent_at"] is not None


async def test_the_source_token_travels_in_the_consent_button_and_is_stored_only_after_consent(h: Harness):
    relax(h)
    await h.say("/start site")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn.site"]
    assert await h.store.get_lead(UID) is None
    await h.press("cn.site")
    assert (await h.store.get_lead(UID)).source == "site"


async def test_a_person_who_consented_before_the_funnel_existed_accepts_the_new_processing_once(h: Harness):
    relax(h)
    h.fake.consent(UID)  # consent given under the old text, in the wrapper
    await h.say("/start yt")
    assert h.last == texts.consent_request()
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn.yt"]
    assert await h.store.get_lead(UID) is None  # nothing is processed until they accept the new text
    await h.press("cn.yt")
    assert (await h.store.get_lead(UID)).source == "yt"
    await h.press(callbacks.reminders("no"))
    await h.say("/start")
    assert "С возвращением" in h.texts[-2] or "Привет!" in h.last  # accepted: no second consent screen


async def test_the_old_features_do_not_need_the_new_acceptance(h: Harness):
    h.fake.consent(UID)
    await h.say("/tariffs")
    assert "Старт" in h.last
    assert await h.store.get_lead(UID) is None


async def test_reminders_opt_in_is_a_separate_explicit_step_and_no_does_not_block(h: Harness):
    relax(h)
    await h.say("/start")
    await h.press(callbacks.consent())
    assert (await h.store.get_lead(UID)).reminders_opt_in is None  # consent is not an opt-in
    await h.press(callbacks.reminders("no"))
    lead = await h.store.get_lead(UID)
    assert lead.reminders_opt_in is False and lead.step == "greet"
    await press(h, "spin_start")
    assert h.last.startswith("Начнём с простого")


@pytest.mark.parametrize("answer,expected", [("yes", True), ("no", False)])
async def test_reminders_answers_are_stored_as_given(h: Harness, answer, expected):
    await join(h, reminders=None)
    await h.press(callbacks.reminders(answer))
    assert (await h.store.get_lead(UID)).reminders_opt_in is expected
