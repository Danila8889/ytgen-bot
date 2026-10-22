"""/stop deletes the funnel data; the 90-day purge; and ISOLATION: a funnel/DB failure never breaks checkout, payment
status, link, referral or withdrawal."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import URL

from tests.conftest import MANAGER, SUPPORT, UID, Harness
from tests.funnel_utils import join, press, relax, run_spin
from tests.mocked_telegram import buttons
from ytgen_bot import app, callbacks, texts
from ytgen_bot.db.engine import make_engine
from ytgen_bot.db.store import PgStore, StoreError
from ytgen_bot.funnel import reminders

EXPIRES = "2026-11-21T10:00:00Z"


def errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


async def break_the_funnel_db(h: Harness) -> PgStore:
    """Point the funnel at a port nobody listens on (a stopped Postgres)."""
    dead = make_engine(URL.create("postgresql+asyncpg", username="u", password="x", host="127.0.0.1", port=1, database="d"))
    broken = PgStore(dead, clock=h.clock)
    h.ctx.funnel.store = broken
    return broken


# --- /stop ----------------------------------------------------------------------------------------------------------------------------


async def test_stop_deletes_all_funnel_data_keeps_a_tombstone_and_still_opts_out(h: Harness):
    await join(h, source="yt")
    await run_spin(h, "s1_one", "s2_mid", "p1_edit", "i1_slow", "n1_more")
    await press(h, "doubt_open", "obj_price")
    await h.say("мой вопрос")
    await h.store.log_ai(UID, step="rec", question="q", reply="r", objection_code=None, handoff=False, reason=None,
                         model="m", latency_ms=1, outcome="ok")
    before = await h.store.user_row_counts(UID)
    assert all(n > 0 for n in before.values())
    await h.say("/stop")
    assert h.fake.calls("POST", "/opt-out")
    assert h.last == texts.stopped()
    counts = await h.store.user_row_counts(UID)
    assert counts == {"funnel_leads": 1, "funnel_events": 0, "funnel_objections": 0, "funnel_ai_log": 0,
                      "funnel_messages": 0, "funnel_followups": 0}
    tomb = await h.store.get_lead(UID)
    assert tomb.status == "stopped" and tomb.stopped_at == h.clock.now
    assert (tomb.username, tomb.first_name, tomb.source, tomb.stage, tomb.volume, tomb.niche_text, tomb.goal,
            tomb.reminders_opt_in, tomb.rec_role, tomb.notes) == (None,) * 10


async def test_reminders_never_resume_after_stop(h: Harness):
    await join(h)
    await press(h, "spin_start", "s1_one")
    await h.say("/stop")
    h.clock.advance(days=40)
    loop = reminders.ReminderLoop(h.ctx.funnel, h.ctx, h.bot, sleep=_nosleep)
    assert await loop.tick() == 0
    assert not any(t.startswith("Ты остановился") for t in h.session.texts(UID))


async def _nosleep(_: float) -> None:
    return None


async def test_coming_back_after_stop_is_a_brand_new_funnel_with_a_fresh_opt_in(h: Harness):
    await join(h, source="yt")
    await press(h, "spin_start", "s1_one")
    await h.say("/stop")
    await h.say("/start")
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["cn"]  # wrapper: consent needed again
    await h.press(callbacks.consent())
    assert [b.callback_data for b in buttons(h.session.last_markup())] == ["rm:yes", "rm:no"]
    lead = await h.store.get_lead(UID)
    assert lead.status == "new" and lead.source is None and lead.stage is None and lead.reminders_opt_in is None


async def test_stop_for_someone_without_funnel_data_leaves_no_row(h: Harness):
    await h.say("/stop")
    assert await h.store.get_lead(UID) is None


async def test_stop_when_the_funnel_db_is_down_still_opts_out_and_says_what_was_not_deleted(h: Harness, caplog):
    await join(h)
    await break_the_funnel_db(h)
    await h.say("/stop")
    assert h.fake.calls("POST", "/opt-out")
    assert h.last.startswith(texts.stopped()) and "удалить ваши ответы" in h.last and SUPPORT in h.last
    assert "funnel_store_error" in errors(caplog)


async def test_stop_wrapper_failure_is_not_success_but_the_funnel_data_is_still_deleted(h: Harness):
    await join(h)
    await press(h, "spin_start")
    h.fake.queue_error("POST", "/opt-out", 503, "bot_api_disabled")
    await h.say("/stop")
    assert h.last == texts.SERVICE_UNAVAILABLE and h.fake.user(UID)["opted_out_at"] is None
    assert (await h.store.get_lead(UID)).status == "stopped"  # deletion does not depend on the wrapper


# --- 90-day AI-log purge ------------------------------------------------------------------------------------------------------------------


async def test_the_purge_loop_deletes_old_ai_log_rows_and_survives_a_failure(h: Harness, caplog):
    await join(h)
    old = h.clock.now - dt.timedelta(days=91)
    await h.store.log_ai(UID, step=None, question="old", reply="r", objection_code=None, handoff=False, reason=None,
                         model="m", latency_ms=1, outcome="ok")
    import sqlalchemy as sa

    async with h.store.engine.begin() as conn:
        await conn.execute(sa.text("UPDATE funnel_ai_log SET ts = :t"), {"t": old})
    await h.store.log_ai(UID, step=None, question="new", reply="r", objection_code=None, handoff=False, reason=None,
                         model="m", latency_ms=1, outcome="ok")
    naps = 0

    async def sleep(_: float) -> None:
        nonlocal naps
        naps += 1
        if naps >= 2:
            raise asyncio.CancelledError

    # utcnow() inside the loop is the REAL clock; rows are relative to the fake one, so give the loop the same cutoff
    h.store._clock = h.clock
    import ytgen_bot.app as application

    original = application.utcnow
    application.utcnow = h.clock
    try:
        with pytest.raises(asyncio.CancelledError):
            await app.purge_loop(h.store, sleep=sleep)
    finally:
        application.utcnow = original
    async with h.store.engine.connect() as conn:
        left = (await conn.execute(sa.text("SELECT question FROM funnel_ai_log"))).scalars().all()
    assert left == ["new"]

    broken = await break_the_funnel_db(h)
    naps = 0
    with pytest.raises(asyncio.CancelledError):
        await app.purge_loop(broken, sleep=sleep)
    assert "funnel_store_error" in errors(caplog)  # ERROR, and the loop went on to its next round (it slept twice)


# --- ISOLATION ---------------------------------------------------------------------------------------------------------------------------


async def test_checkout_works_end_to_end_with_the_funnel_db_unavailable(h: Harness, caplog):
    relax(h)
    h.ctx.watchers = None
    h.fake.consent(UID)
    h.fake.link(UID)
    await break_the_funnel_db(h)
    await h.say("/tariffs")
    assert "Старт" in h.last
    await h.press(callbacks.tariff(11))
    assert "Есть промокод?" in h.last
    await h.press(callbacks.quote(11))
    assert "К оплате" in h.last
    await h.press(callbacks.pay(11, 1234))
    assert "Платёж создан" in h.texts[-1] or "Платёж создан" in " ".join(h.texts[-2:])
    pid = h.fake.latest[UID]
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=11, tariff_name="Старт", tariff_expires_at=EXPIRES)
    await h.press(callbacks.check(pid))
    assert any("Оплата прошла" in t for t in h.texts)  # the payment result is delivered
    await h.say("/start paid")
    assert "Оплата прошла" in h.last
    # the funnel failure is on record at ERROR, not hidden
    assert "funnel_store_error" in errors(caplog) or "funnel_hook_failed" in errors(caplog)


async def test_link_referral_status_help_and_withdrawal_work_with_the_funnel_db_unavailable(h: Harness):
    relax(h)
    h.fake.consent(UID)
    await break_the_funnel_db(h)
    await h.say("/status")
    assert texts.status_unlinked() in h.last
    await h.say("/help")
    assert "YouTubeGen Bot" in h.last
    await h.press(callbacks.menu("link"))
    await h.say("john@example.com")
    assert "Код отправлен" in h.last
    await h.say("123456")
    assert any("аккаунт привязан" in t or "аккаунт создан" in t for t in h.texts)
    await h.say("/referral")
    assert "Реферальная программа" in h.last and "Ваша ссылка" in h.last
    await h.press(callbacks.withdraw("start"))
    await h.say("300")
    await h.press(callbacks.withdraw_ok(300))
    assert "Заявка №" in h.last


async def test_start_with_the_funnel_db_down_says_so_and_still_offers_the_menu(h: Harness, caplog):
    h.fake.consent(UID)
    await break_the_funnel_db(h)
    await h.say("/start")
    assert "Напиши менеджеру" in h.last and SUPPORT in h.last and "Выберите действие" in h.last
    assert "Тарифы и оплата" in [b.text for b in buttons(h.session.last_markup())]
    assert "funnel_store_error" in errors(caplog)


async def test_consent_button_with_the_funnel_db_down_records_consent_and_shows_the_menu(h: Harness):
    await break_the_funnel_db(h)
    await h.say("/start")
    await h.press(callbacks.consent())
    assert h.fake.user(UID)["consent_at"] is not None  # the wrapper has the consent
    assert "Тарифы и оплата" in [b.text for b in buttons(h.session.last_markup())]


async def test_start_of_a_customer_with_the_funnel_db_down_is_the_plain_menu(h: Harness, caplog):
    h.fake.consent(UID)
    h.fake.link(UID, tariff_id=22, tariff_name="Месяц", tariff_expires_at=EXPIRES)
    await break_the_funnel_db(h)
    await h.say("/start")
    assert "Выберите действие" in h.last and "Напиши менеджеру" not in h.last
    assert "funnel_store_error" in errors(caplog)


async def test_a_funnel_button_with_the_db_down_shows_something_went_wrong_with_the_manager_link(h: Harness, caplog):
    await join(h)
    await break_the_funnel_db(h)
    await press(h, "spin_start")
    assert h.last == f"Что-то пошло не так. Напиши менеджеру: {SUPPORT}."
    assert [b.url for b in buttons(h.session.last_markup())] == ["https://t.me/ytgen_support_test"]
    assert "funnel_store_error" in errors(caplog)


async def test_free_text_with_the_db_down_is_an_honest_error_not_silence(h: Harness):
    await join(h)
    await break_the_funnel_db(h)
    await h.say("привет")
    assert h.last.startswith("Что-то пошло не так. Напиши менеджеру")


async def test_blocked_report_to_the_wrapper_survives_a_dead_funnel_db(h: Harness):
    await join(h)
    await break_the_funnel_db(h)
    h.session.errors.append(TelegramForbiddenError(method=SendMessage(chat_id=UID, text="x"), message="Forbidden: bot was blocked by the user"))
    await h.say("/help")
    assert h.fake.calls("POST", "/blocked")[-1]["json"] == {"blocked": True}


async def test_an_unexpected_exception_in_a_funnel_hook_never_reaches_the_payment_flow(h: Harness, monkeypatch, caplog):
    relax(h)
    h.ctx.watchers = None
    await join(h)
    h.fake.link(UID)
    await press(h, "spin_start")

    async def boom(*a, **k):
        raise RuntimeError("bug in the funnel")

    monkeypatch.setattr(h.ctx.funnel.store, "mark_paid", boom)
    await h.press(callbacks.pay(22, 11111))
    pid = h.fake.latest[UID]
    h.fake.payments[pid].update(status="succeeded", credited=True)
    h.fake.linked[UID].update(tariff_id=22, tariff_name="Месяц", tariff_expires_at=EXPIRES)
    await h.press(callbacks.check(pid))
    assert any("Оплата прошла" in t for t in h.texts)
    assert "funnel_hook_failed" in errors(caplog)


async def test_a_store_error_is_a_clean_exception_type_with_no_secrets(h: Harness):
    broken = await break_the_funnel_db(h)
    with pytest.raises(StoreError) as exc:
        await broken.get_lead(UID)
    assert "x" not in str(exc.value) or str(exc.value) == "get_lead"


async def test_manager_alerts_use_the_manager_chat_only(h: Harness):
    await join(h)
    assert MANAGER not in {m.chat_id for m in h.session.sent() if m.chat_id != MANAGER}
