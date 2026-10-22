"""Privacy rules: free text never reaches a log line, people are identified by Telegram id only, events hold no free text."""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa

from tests.conftest import UID, Harness
from tests.funnel_utils import join, press

SENTINELS = ["СЕКРЕТНАЯ-ФРАЗА-ниша-кроликов", "ПРОБНЫЙ-ВОПРОС-про-цены-9988", "ЖАЛОБА-на-всё-абракадабра"]
NAME = "Конфиденциальнаяфамилия"
HANDLE = "secret_handle_xyz"


def everything_logged(caplog) -> str:
    return "\n".join(f"{r.levelname} {r.getMessage()} {sorted(r.__dict__.items(), key=lambda kv: kv[0])}" for r in caplog.records)


async def test_free_text_and_names_never_reach_any_log_line_and_events_hold_no_free_text(h: Harness, caplog):
    caplog.set_level(logging.DEBUG)
    await join(h)
    await h.say("/start", username=HANDLE, first_name=NAME)
    await press(h, "spin_start", "s1_one", "s2_mid")
    await h.say(SENTINELS[0], username=HANDLE, first_name=NAME)  # S3: stored as the answer
    h.ai.say("Ответ про цены.")
    await h.say(SENTINELS[1], username=HANDLE, first_name=NAME)  # a free question
    h.ai.fail("failed")
    await h.say("ещё " + SENTINELS[1])
    await h.say(SENTINELS[2] + " обман развод")  # a trigger: hand-off with a card
    logs = everything_logged(caplog)
    for secret in (*SENTINELS, NAME, HANDLE):
        assert secret not in logs, secret
    # events: button codes and short tokens only
    async with h.store.engine.connect() as conn:
        values = (await conn.execute(sa.text("SELECT event, value FROM funnel_events"))).all()
    assert values
    for event, value in values:
        assert re.fullmatch(r"[a-z0-9_:\-]{1,40}", event), event
        assert value is None or re.fullmatch(r"[a-z0-9_:\-]{1,40}", value), (event, value)
        assert not any(s in (value or "") for s in SENTINELS)
    # the free text lives only where the owner decided: the lead's own answers and the rolling buffer of five
    lead = await h.store.get_lead(UID)
    assert lead.niche_text == SENTINELS[0]
    assert len(await h.store.recent_messages(UID)) <= 5


async def test_funnel_log_lines_identify_people_by_telegram_id_only(h: Harness, caplog):
    caplog.set_level(logging.DEBUG)
    await join(h)
    await h.say("/start", username=HANDLE, first_name=NAME)
    await press(h, "spin_start")
    funnel_records = [r for r in caplog.records if r.name.startswith("ytgen_bot")]
    assert funnel_records
    assert any(getattr(r, "tg_user", None) == UID for r in funnel_records)
    for r in funnel_records:
        extra = {k: v for k, v in r.__dict__.items() if k in ("username", "first_name", "email", "text", "question")}
        assert extra == {}


async def test_the_ai_question_is_stored_in_ai_log_but_never_logged(h: Harness, caplog):
    """ai_log is the owner's weekly-review table (kept 90 days, deleted on /stop); log lines never carry the question."""
    caplog.set_level(logging.DEBUG)
    await join(h)
    await h.store.log_ai(UID, step="s2", question=SENTINELS[1], reply="r", objection_code=None, handoff=False,
                         reason=None, model="m", latency_ms=1, outcome="ok")
    assert SENTINELS[1] not in everything_logged(caplog)
    async with h.store.engine.connect() as conn:
        assert (await conn.execute(sa.text("SELECT question FROM funnel_ai_log"))).scalar_one() == SENTINELS[1]
