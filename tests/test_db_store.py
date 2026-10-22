"""The funnel database against a real (ephemeral) Postgres: migrations, retention, at-most-once claims."""
from __future__ import annotations

import ast
import datetime as dt
import logging
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy import URL

from tests.clock import FakeClock
from ytgen_bot.db import migrate, schema
from ytgen_bot.db.engine import make_engine
from ytgen_bot.db.store import PgStore, StoreError

UID = 5001


async def lead(store: PgStore, uid: int = UID, source: str | None = None):
    return await store.upsert_lead(uid, username="u", first_name="Имя", source=source)


# --- migrations ---------------------------------------------------------------------------------


async def test_migrations_are_at_head_and_idempotent(store: PgStore):
    assert await migrate.at_head(store.engine)
    await migrate.upgrade_to_head(store.engine)  # a second run is a no-op, not an error
    assert await migrate.current_revision(store.engine) == migrate.head_revision()


async def test_migrated_schema_matches_the_table_definitions(store: PgStore):
    def reflect(conn: sa.Connection) -> dict[str, set[str]]:
        insp = sa.inspect(conn)
        return {name: {c["name"] for c in insp.get_columns(name)} for name in insp.get_table_names()}

    async with store.engine.connect() as conn:
        actual = await conn.run_sync(reflect)
    actual.pop("alembic_version", None)
    expected = {name: {c.name for c in table.c} for name, table in schema.metadata.tables.items()}
    assert actual == expected


def test_migrations_are_additive_only():
    """A rollback to the previous image must run on the migrated schema: no drops, renames or retyping."""
    forbidden = {"drop_table", "drop_column", "drop_constraint", "drop_index", "alter_column", "rename_table",
                 "execute"}
    versions = pathlib.Path(migrate.MIGRATIONS_DIR) / "versions"
    seen = 0
    for path in versions.glob("*.py"):
        if path.name == "__init__.py":
            continue
        seen += 1
        calls = {
            n.func.attr for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not calls & forbidden, (path.name, calls & forbidden)
    assert seen >= 1


async def test_concurrent_migration_runs_do_not_fail(store: PgStore):
    import asyncio

    await asyncio.gather(*[migrate.upgrade_to_head(store.engine) for _ in range(3)])
    assert await migrate.at_head(store.engine)


# --- leads ---------------------------------------------------------------------------------------


async def test_source_is_first_touch_and_never_overwritten(store: PgStore):
    first = await lead(store, source="yt")
    again = await store.upsert_lead(UID, username="new", first_name="Новое", source="tg")
    assert first.source == "yt" and again.source == "yt"
    assert again.username == "new" and again.first_name == "Новое"  # names refresh, source does not


async def test_unknown_lead_columns_are_refused(store: PgStore):
    await lead(store)
    with pytest.raises(ValueError):
        await store.update_lead(UID, tg_id=1)


async def test_message_buffer_keeps_the_last_five_in_order(store: PgStore):
    await lead(store)
    for i in range(8):
        await store.push_message(UID, f"m{i}")
    await store.push_message(UID + 1, "other user")
    assert await store.recent_messages(UID) == ["m3", "m4", "m5", "m6", "m7"]
    assert await store.recent_messages(UID + 1) == ["other user"]


async def test_one_pending_followup_per_user_and_replace(store: PgStore, clock: FakeClock):
    await lead(store)
    due = clock.now + dt.timedelta(hours=24)
    await store.replace_followup(UID, "f1", due)
    await store.replace_followup(UID, "f1", due + dt.timedelta(hours=1))  # replaces, never duplicates
    pending = await store.pending_followup(UID)
    assert pending is not None and pending.due_at == due + dt.timedelta(hours=1)
    await store.replace_followup(UID, None, None)
    assert await store.pending_followup(UID) is None


async def test_claim_is_at_most_once(store: PgStore, clock: FakeClock):
    await lead(store)
    await store.replace_followup(UID, "f1", clock.now + dt.timedelta(hours=1))
    assert await store.claim_due_followups(clock.now, 10) == []  # not due yet
    clock.advance(hours=2)
    first = await store.claim_due_followups(clock.now, 10)
    second = await store.claim_due_followups(clock.now, 10)
    assert [f.kind for f in first] == ["f1"] and second == []  # claimed once; a restart cannot re-send it
    await store.finish_followup(first[0].id, "sent")


async def test_paid_transition_and_single_announcement(store: PgStore, clock: FakeClock):
    await lead(store)
    await store.replace_followup(UID, "f1", clock.now + dt.timedelta(hours=1))
    assert await store.mark_paid(UID, tariff="Pro", by_manager=False) is True
    assert await store.mark_paid(UID, tariff="Pro", by_manager=False) is False  # only the transition counts
    assert await store.pending_followup(UID) is None  # paid cancels the schedule
    assert [await store.claim_paid_announcement(UID) for _ in range(3)] == [True, False, False]
    got = await store.get_lead(UID)
    assert got is not None and got.status == "paid" and got.paid_tariff == "Pro" and got.paid_at is not None


async def test_mark_paid_without_a_lead_does_nothing(store: PgStore):
    assert await store.mark_paid(UID, tariff=None, by_manager=True) is False
    assert await store.get_lead(UID) is None


# --- retention -----------------------------------------------------------------------------------


async def fill(store: PgStore, uid: int, clock: FakeClock) -> None:
    await store.upsert_lead(uid, username="name", first_name="Имя", source="yt")
    await store.update_lead(uid, stage="s1_one", niche_text="котики", reminders_opt_in=True, status="in_spin")
    await store.add_event(uid, "btn", "s1_one")
    await store.add_objection(uid, "obj_price")
    await store.push_message(uid, "free text")
    await store.log_ai(uid, step="s2", question="q", reply="r", objection_code=None, handoff=False, reason=None,
                       model="m", latency_ms=5, outcome="ok")
    await store.replace_followup(uid, "f1", clock.now + dt.timedelta(hours=24))


async def test_stop_deletes_everything_of_that_user_and_keeps_only_a_tombstone(store: PgStore, clock: FakeClock):
    await fill(store, UID, clock)
    await fill(store, UID + 1, clock)
    result = await store.purge_user(UID)
    assert result.had_data
    counts = await store.user_row_counts(UID)
    assert counts == {"funnel_leads": 1, "funnel_events": 0, "funnel_objections": 0, "funnel_ai_log": 0,
                      "funnel_messages": 0, "funnel_followups": 0}
    tomb = await store.get_lead(UID)
    assert tomb is not None and tomb.is_tombstone and tomb.stopped_at == clock.now
    # nothing personal survives in the tombstone
    assert (tomb.username, tomb.first_name, tomb.source, tomb.stage, tomb.niche_text, tomb.reminders_opt_in,
            tomb.step, tomb.notes) == (None, None, None, None, None, None, None, None)
    # another user is untouched
    assert (await store.user_row_counts(UID + 1))["funnel_events"] == 1


async def test_stop_for_a_user_without_funnel_data_writes_nothing(store: PgStore):
    result = await store.purge_user(UID)
    assert not result.had_data and await store.get_lead(UID) is None


async def test_a_tombstoned_user_who_returns_starts_from_a_blank_lead_without_reminders(store: PgStore, clock: FakeClock):
    await fill(store, UID, clock)
    await store.purge_user(UID)
    fresh = await store.upsert_lead(UID, username="x", first_name="Y", source=None)
    assert fresh.status == "new" and fresh.source is None and fresh.reminders_opt_in is None
    assert fresh.stopped_at is None and await store.pending_followup(UID) is None


async def test_ai_log_purge_deletes_only_rows_older_than_the_cutoff(store: PgStore, clock: FakeClock):
    await lead(store)
    for days in (120, 91, 89, 1):
        await store.log_ai(UID, step=None, question=f"q{days}", reply="r", objection_code=None, handoff=False,
                           reason=None, model="m", latency_ms=1, outcome="ok")
        clock.advance(days=0)  # same instant; rewrite ts below to simulate age
    async with store.engine.begin() as conn:
        for row_id, days in zip((1, 2, 3, 4), (120, 91, 89, 1), strict=True):
            await conn.execute(sa.text("UPDATE funnel_ai_log SET ts = :t WHERE id = :i"),
                               {"t": clock.now - dt.timedelta(days=days), "i": row_id})
    removed = await store.purge_ai_log(clock.now - dt.timedelta(days=90))
    assert removed == 2
    again = await store.purge_ai_log(clock.now - dt.timedelta(days=90))
    assert again == 0  # idempotent
    async with store.engine.connect() as conn:
        left = (await conn.execute(sa.text("SELECT question FROM funnel_ai_log ORDER BY id"))).scalars().all()
    assert left == ["q89", "q1"]


async def test_ai_usage_counts_only_real_calls(store: PgStore, clock: FakeClock):
    await lead(store)
    for outcome in ("ok", "validator_blocked", "parse_failed", "provider_error", "rate_limited", "ceiling"):
        await store.log_ai(UID, step=None, question="q", reply=None, objection_code=None, handoff=False,
                           reason=None, model=None, latency_ms=None, outcome=outcome)
    since = clock.now - dt.timedelta(hours=1)
    assert await store.ai_calls_since(since) == 4
    assert await store.ai_calls_since(since, uid=UID + 9) == 0
    assert await store.ceiling_logged_since(since) is True
    with pytest.raises(ValueError):
        await store.log_ai(UID, step=None, question="q", reply=None, objection_code=None, handoff=False,
                           reason=None, model=None, latency_ms=None, outcome="nonsense")


async def test_report_data_shape(store: PgStore, clock: FakeClock):
    await fill(store, UID, clock)
    await store.add_event(UID, "spin_done")
    await store.add_event(UID, "recommended", "base")
    await store.add_event(UID, "link_sent")
    await store.update_lead(UID, handoff_reason="payment_method")
    data = await store.report_data()
    assert data["started"] == 1 and data["spin_done"] == 1 and data["recommended"] == 1 and data["link_sent"] == 1
    assert data["objections"] == [{"code": "obj_price", "leads": 1, "reached_link": 1}]
    assert data["handoff_reasons"] == [{"reason": "payment_method", "n": 1}]
    assert data["sources"] == [{"source": "yt", "leads": 1, "paid": 0}]


# --- failure: StoreError, ERROR log, no secrets ---------------------------------------------------


async def test_an_unreachable_database_raises_store_error_and_logs_error(caplog: pytest.LogCaptureFixture):
    dead = make_engine(URL.create("postgresql+asyncpg", username="u", password="SECRET-PW", host="127.0.0.1",
                                  port=1, database="d"))
    broken = PgStore(dead)
    try:
        with pytest.raises(StoreError):
            await broken.get_lead(UID)
        with pytest.raises(StoreError):
            await broken.ping()
    finally:
        await dead.dispose()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.getMessage() == "funnel_store_error"]
    assert len(errors) == 2
    assert all("SECRET-PW" not in r.getMessage() and "SECRET-PW" not in str(r.__dict__) for r in caplog.records)
