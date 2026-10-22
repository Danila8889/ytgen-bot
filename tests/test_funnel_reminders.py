"""Reminders F1-F3 (PDF section 9): the pure schedule, the DB-backed loop, at-most-once delivery, cancellation rules."""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

import httpx
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from tests.clock import START
from tests.conftest import UID, Harness
from tests.funnel_utils import codes_of, join, press, run_spin
from ytgen_bot.formatting import rub
from ytgen_bot.funnel import reminders
from ytgen_bot.funnel.model import Lead, Status

H = dt.timedelta(hours=1)
D = dt.timedelta(days=1)


def make_lead(**over) -> Lead:
    base: dict[str, object] = {
        "tg_id": 1, "username": None, "first_name": None, "source": None, "status": "in_spin", "step": "s2",
        "stage": None, "volume": None, "niche_text": None, "pain": None, "pain_text": None, "impact": None,
        "goal": None, "rec_role": None, "rec_tariff_id": None, "handoff_reason": None, "handoff_at": None,
        "handoff_priority": None, "remind_ok": False, "reminders_opt_in": True, "followups_sent": 0,
        "answered_followup": False, "last_followup_at": None, "unsub": False, "paid_tariff": None, "paid_at": None,
        "paid_by_manager": False, "paid_announced": False, "pay_check_sent": False, "notes": None,
        "last_activity_at": START, "created_at": START, "updated_at": START, "stopped_at": None,
    }
    base.update(over)
    return Lead(**base)  # type: ignore[arg-type]


# --- the pure schedule ----------------------------------------------------------------------------------------------------------


def test_f1_is_due_24_hours_after_the_last_activity_for_spin_and_recommended_leads():
    for status in ("in_spin", "recommended"):
        assert reminders.next_followup(make_lead(status=status)) == ("f1", START + 24 * H)


def test_nothing_is_scheduled_without_the_opt_in_or_after_unsub():
    assert reminders.next_followup(make_lead(reminders_opt_in=None)) is None
    assert reminders.next_followup(make_lead(reminders_opt_in=False)) is None
    assert reminders.next_followup(make_lead(unsub=True)) is None


def test_nothing_is_scheduled_for_new_handoff_paid_cold_blocked_or_stopped_leads():
    for status in ("new", "handoff", "paid", "cold", "blocked", "stopped"):
        assert reminders.next_followup(make_lead(status=status)) is None, status


def test_f2_and_f3_need_remind_ok_or_an_answer_to_the_previous_reminder():
    f1_at = START + 24 * H
    sent1 = make_lead(followups_sent=1, last_followup_at=f1_at, last_activity_at=START)
    assert reminders.next_followup(sent1) is None  # silent after F1: no F2
    assert reminders.next_followup(dataclasses.replace(sent1, remind_ok=True)) == ("f2", f1_at + 3 * D)
    assert reminders.next_followup(dataclasses.replace(sent1, answered_followup=True)) == ("f2", f1_at + 3 * D)
    f2_at = f1_at + 3 * D
    sent2 = make_lead(followups_sent=2, last_followup_at=f2_at, remind_ok=True, last_activity_at=START)
    assert reminders.next_followup(sent2) == ("f3", f2_at + 7 * D)
    assert reminders.next_followup(dataclasses.replace(sent2, followups_sent=3)) is None  # three, no more


def test_f2_is_never_sooner_than_24_hours_after_the_last_activity():
    f1_at = START + 24 * H
    active_just_before = f1_at + 3 * D - H
    lead = make_lead(followups_sent=1, last_followup_at=f1_at, answered_followup=True, last_activity_at=active_just_before)
    assert reminders.next_followup(lead) == ("f2", active_just_before + 24 * H)


def test_m53_only_for_link_sent_leads_who_opted_in_and_only_once():
    lead = make_lead(status="link_sent")
    assert reminders.next_followup(lead) == ("m53", START + dt.timedelta(minutes=30))
    assert reminders.next_followup(dataclasses.replace(lead, pay_check_sent=True)) is None
    assert reminders.next_followup(dataclasses.replace(lead, reminders_opt_in=False)) is None


# --- the DB-backed loop --------------------------------------------------------------------------------------------------------------


async def nosleep(_: float) -> None:
    return None


def loop_for(h: Harness, sleep=nosleep) -> reminders.ReminderLoop:
    return reminders.ReminderLoop(h.ctx.funnel, h.ctx, h.bot, sleep=sleep)


async def stalled_at_s2(h: Harness, **kw) -> Harness:
    await join(h, **kw)
    await press(h, "spin_start", "s1_one")
    return h


async def test_f1_goes_out_once_24_hours_after_the_last_activity_with_its_buttons(h: Harness):
    await stalled_at_s2(h)
    loop = loop_for(h)
    h.clock.advance(hours=23)
    assert await loop.tick() == 0
    h.clock.advance(hours=2)
    assert await loop.tick() == 1
    assert h.last == "Ты остановился на подборе тарифа. Осталось пара вопросов, вернёмся?"
    assert codes_of(h) == ["resume", "end_polite", "unsub"]
    h.clock.advance(days=10)
    assert await loop.tick() == 0  # F2/F3 need remind_ok or an answer: nothing for a silent person
    lead = await h.store.get_lead(UID)
    assert lead.followups_sent == 1 and lead.status == "in_spin"


async def test_delivery_is_at_most_once_across_a_restart(h: Harness):
    await stalled_at_s2(h)
    h.clock.advance(days=2)
    row = (await h.store.claim_due_followups(h.clock.now, 10))[0]  # claimed, then the process "crashes" before sending
    assert row.kind == "f1"
    assert await loop_for(h).tick() == 0  # after the restart the row is not due again: never sent twice
    assert not any(t.startswith("Ты остановился") for t in h.session.texts(UID))


async def test_any_activity_cancels_the_pending_reminder_and_restarts_the_clock(h: Harness):
    await stalled_at_s2(h)
    h.clock.advance(hours=20)
    await press(h, "s2_mid")  # activity
    pending = await h.store.pending_followup(UID)
    assert pending.due_at == h.clock.now + 24 * H
    h.clock.advance(hours=5)
    assert await loop_for(h).tick() == 0


async def test_f2_and_f3_follow_for_someone_who_answered_f1_with_the_prescribed_gaps(h: Harness):
    await stalled_at_s2(h)
    loop = loop_for(h)
    h.clock.advance(days=1, hours=1)
    assert await loop.tick() == 1  # F1
    f1_at = h.clock.now
    await press(h, "resume")  # answered F1
    h.clock.advance(days=2)
    assert await loop.tick() == 0
    h.clock.advance(days=1, hours=1)  # 3 days after F1
    assert await loop.tick() == 1
    assert "посмотри примеры видео" in h.last and h.last.count("<a href") == 4 and h.clock.now >= f1_at + 3 * D
    assert codes_of(h) == ["ask_free", "spin_start", "unsub"]
    f2_at = h.clock.now
    h.clock.advance(days=6)
    assert await loop.tick() == 0
    h.clock.advance(days=1, hours=1)  # 7 days after F2
    assert await loop.tick() == 1
    assert h.clock.now >= f2_at + 7 * D and "Последнее напоминание от меня" in h.last
    assert f"<b>Пробный</b> за {rub(4321)} на 7 дн." in h.last  # the price is from the wrapper, never invented
    assert codes_of(h) == ["try_lite", "handoff_manual", "unsub"]
    assert (await h.store.get_lead(UID)).status == "cold"  # after F3 the dialogue closes
    h.clock.advance(days=30)
    assert await loop.tick() == 0


async def test_remind_ok_enables_f2_without_answering_f1(h: Harness):
    await join(h)
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")
    await press(h, "doubt_open", "obj_think", "remind_ok")
    loop = loop_for(h)
    h.clock.advance(days=1, hours=1)
    assert await loop.tick() == 1  # F1
    h.clock.advance(days=3, hours=1)
    assert await loop.tick() == 1  # F2 (remind_ok pressed)
    assert "посмотри примеры видео" in h.last


async def test_no_invented_scarcity_in_any_reminder(h: Harness):
    await stalled_at_s2(h)
    loop = loop_for(h)
    await press(h, "remind_ok")
    for _ in range(3):
        h.clock.advance(days=8)
        await loop.tick()
    texts_sent = [t for t in h.session.texts(UID) if "Ты остановился" in t or "посмотри примеры" in t or "Последнее напоминание" in t]
    assert len(texts_sent) == 3
    for text in texts_sent:
        for bad in ("осталось", "места", "до вечера", "только сегодня", "успей", "%", "скидк"):
            assert bad not in text.replace("Осталось пара вопросов", "")


async def test_no_reminders_without_the_opt_in_and_unsub_cancels_them(h: Harness):
    await stalled_at_s2(h, reminders=False)
    assert await h.store.pending_followup(UID) is None
    await h.press("rm:yes")
    assert await h.store.pending_followup(UID) is not None
    await press(h, "unsub")
    assert await h.store.pending_followup(UID) is None
    h.clock.advance(days=30)
    assert await loop_for(h).tick() == 0
    assert (await h.store.get_lead(UID)).unsub is True


async def test_handoff_cancels_reminders(h: Harness):
    await stalled_at_s2(h)
    assert await h.store.pending_followup(UID) is not None
    await press(h, "handoff_manual")
    assert await h.store.pending_followup(UID) is None


async def test_a_blocked_bot_marks_the_lead_blocked_reports_to_the_wrapper_and_is_not_tried_again(h: Harness):
    await stalled_at_s2(h)
    h.clock.advance(days=2)
    h.session.errors.append(TelegramForbiddenError(method=SendMessage(chat_id=UID, text="x"), message="Forbidden: bot was blocked by the user"))
    assert await loop_for(h).tick() == 0
    assert h.fake.calls("POST", "/blocked")[-1]["json"] == {"blocked": True}
    lead = await h.store.get_lead(UID)
    assert lead.status == Status.BLOCKED
    assert await h.store.pending_followup(UID) is None
    h.clock.advance(days=30)
    assert await loop_for(h).tick() == 0 and len(h.fake.calls("POST", "/blocked")) == 1


async def test_a_person_who_withdrew_consent_elsewhere_gets_nothing(h: Harness):
    await stalled_at_s2(h)
    h.fake.user(UID)["opted_out_at"] = "2026-10-22T11:00:00Z"
    h.clock.advance(days=2)
    assert await loop_for(h).tick() == 0
    assert not any(t.startswith("Ты остановился") for t in h.session.texts(UID))


async def test_a_wrapper_outage_defers_the_reminder_and_logs_an_error_instead_of_guessing(h: Harness, caplog):
    await stalled_at_s2(h)
    h.clock.advance(days=2)
    h.fake.queue("GET", "/me", httpx.ConnectError("down"))
    assert await loop_for(h).tick() == 0
    assert any(r.levelno == logging.ERROR and r.getMessage() == "followup_wrapper_check_failed" for r in caplog.records)
    pending = await h.store.pending_followup(UID)
    assert pending is not None and pending.kind == "f1" and pending.due_at == h.clock.now + dt.timedelta(minutes=10)
    h.clock.advance(minutes=11)
    assert await loop_for(h).tick() == 1


async def test_a_lead_who_became_paid_gets_no_reminder_and_is_marked_paid_silently(h: Harness):
    await stalled_at_s2(h)
    h.fake.link(UID, tariff_id=22, tariff_name="Месяц", tariff_expires_at="2026-11-21T10:00:00Z")
    h.clock.advance(days=2)
    before = len(h.session.texts(UID))
    assert await loop_for(h).tick() == 0
    assert len(h.session.texts(UID)) == before and (await h.store.get_lead(UID)).status == "paid"


async def test_sends_are_paced(h: Harness):
    for uid in (3001, 3002, 3003):
        await h.say("/start", uid=uid)
        await h.press("cn", uid=uid)
        await h.press("rm:yes", uid=uid)
        await h.press("f:spin_start", uid=uid)
    h.clock.advance(days=2)
    naps: list[float] = []

    async def sleep(seconds: float) -> None:
        naps.append(seconds)

    assert await loop_for(h, sleep).tick() == 3
    assert naps == [reminders.SEND_PACE_S] * 3 and reminders.SEND_PACE_S >= 0.1  # well under Telegram's limits
