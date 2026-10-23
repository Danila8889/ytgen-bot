"""The deploy self-check (DB at head, tariff contract, no live LLM call), startup isolation, and the operator CLIs."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
import pytest
import sqlalchemy as sa
from aiogram import Bot
from sqlalchemy import URL

from tests.conftest import KIE_KEY, MANAGER, make_settings
from tests.fake_wrapper import SERVICE_KEY, FakeWrapper, funnel_tariffs
from tests.mocked_telegram import BOT_TOKEN, MockedSession
from ytgen_bot import app, cli
from ytgen_bot.__main__ import main
from ytgen_bot.db.engine import make_engine
from ytgen_bot.wrapper import WrapperClient


def settings_for(url: URL, **over):
    return make_settings(
        db_host=url.host, db_port=url.port, db_name=url.database, db_user=url.username, db_password=url.password,
        **over,
    )


def wrapper_with(fake: FakeWrapper) -> WrapperClient:
    return WrapperClient("https://wrapper.example.test", SERVICE_KEY, transport=fake.transport())


async def create_database(url: URL, name: str) -> URL:
    engine = make_engine(url)
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()
    return url.set(database=name)


# --- funnel_self_check ---------------------------------------------------------------------------------------------------------------


async def test_funnel_self_check_passes_with_a_migrated_db_and_a_valid_tariff_contract(pg_url):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    wrapper = wrapper_with(fake)
    await app.funnel_self_check(settings_for(pg_url), wrapper)
    await wrapper.aclose()
    assert fake.calls("GET", "/v1/public/tariffs") and not fake.calls("GET", "/me")
    assert not any("x-bot-service-key" in c["headers"] for c in fake.calls("GET", "/v1/public/tariffs"))


async def test_funnel_self_check_fails_when_the_database_is_not_reachable():
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match="funnel database is not reachable"):
        await app.funnel_self_check(make_settings(db_port=1), wrapper)
    await wrapper.aclose()


async def test_funnel_self_check_fails_when_migrations_are_not_at_head(pg_url):
    empty = await create_database(pg_url, "never_migrated")
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match="migrations are not at head"):
        await app.funnel_self_check(settings_for(empty), wrapper)
    await wrapper.aclose()


@pytest.mark.parametrize("drop,message", [("is_monthly", "no is_monthly"), ("capacity", "no capacity")])
async def test_funnel_self_check_fails_on_a_public_row_without_is_monthly_or_capacity(pg_url, drop, message):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    del fake.tariffs[1][drop]
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match=f"wrapper tariff contract: .*{message}"):
        await app.funnel_self_check(settings_for(pg_url), wrapper)
    await wrapper.aclose()


async def test_funnel_self_check_reads_the_public_list_not_the_per_user_one(pg_url):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    fake.queue_error("GET", "/v1/public/tariffs", 500, "boom")
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match="could not read the wrapper's public tariffs"):
        await app.funnel_self_check(settings_for(pg_url), wrapper)
    await wrapper.aclose()


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(s: float) -> None:
        recorded.append(s)

    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    return recorded


async def test_funnel_self_check_retries_a_flaky_public_tariff_read_then_passes(pg_url, sleeps, caplog):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    fake.queue("GET", "/v1/public/tariffs", httpx.ConnectTimeout("slow"))
    fake.queue("GET", "/v1/public/tariffs", httpx.ConnectError("reset"))
    wrapper = wrapper_with(fake)
    await app.funnel_self_check(settings_for(pg_url), wrapper, retry_delays=(1.0, 2.0, 3.0))
    await wrapper.aclose()
    assert len(fake.calls("GET", "/v1/public/tariffs")) == 3 and sleeps == [1.0, 2.0]
    retries = [r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == "self_check_retry"]
    assert [r.attempt for r in retries] == [1, 2]


async def test_funnel_self_check_gives_up_on_a_persistently_unreachable_wrapper_and_says_how_often(pg_url, sleeps):
    fake = FakeWrapper()
    for _ in range(4):
        fake.queue("GET", "/v1/public/tariffs", httpx.ConnectTimeout("slow"))
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match="wrapper public tariffs unreachable after 3 attempts"):
        await app.funnel_self_check(settings_for(pg_url), wrapper, retry_delays=(1.0, 2.0))
    await wrapper.aclose()
    assert len(fake.calls("GET", "/v1/public/tariffs")) == 3


async def test_funnel_self_check_never_retries_a_database_or_contract_error(pg_url, sleeps):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    wrapper = wrapper_with(fake)
    with pytest.raises(app.SelfCheckError, match="funnel database is not reachable"):
        await app.funnel_self_check(make_settings(db_port=1), wrapper, retry_delays=(1.0, 2.0))
    assert not fake.requests and sleeps == []
    del fake.tariffs[1]["capacity"]
    with pytest.raises(app.SelfCheckError, match="wrapper tariff contract"):
        await app.funnel_self_check(settings_for(pg_url), wrapper, retry_delays=(1.0, 2.0))
    fake.queue_error("GET", "/v1/public/tariffs", 500, "boom")
    with pytest.raises(app.SelfCheckError, match="could not read the wrapper's public tariffs"):
        await app.funnel_self_check(settings_for(pg_url), wrapper, retry_delays=(1.0, 2.0))
    await wrapper.aclose()
    assert len(fake.calls("GET", "/v1/public/tariffs")) == 2 and sleeps == []


async def test_wrong_role_shape_is_a_loud_error_log_but_not_a_deploy_blocker(pg_url, caplog):
    fake = FakeWrapper()
    fake.tariffs = [t for t in funnel_tariffs() if t["id"] != 24]  # only two monthly tariffs
    wrapper = wrapper_with(fake)
    await app.funnel_self_check(settings_for(pg_url), wrapper)
    await wrapper.aclose()
    assert any(r.levelno == logging.ERROR and r.getMessage() == "funnel_tariff_roles_invalid" for r in caplog.records)


async def test_the_self_check_makes_no_llm_call(pg_url, monkeypatch):
    """Config presence only: nothing in the self-check path constructs or calls the kie.ai client."""
    import ytgen_bot.ai.client as ai_client

    def boom(*a, **k):
        raise AssertionError("the self-check must not call the LLM provider")

    monkeypatch.setattr(ai_client.KieChatClient, "chat", boom)
    monkeypatch.setattr(app, "KieChatClient", boom)
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    wrapper = wrapper_with(fake)
    await app.funnel_self_check(settings_for(pg_url), wrapper)
    await wrapper.aclose()


async def test_run_selfcheck_runs_both_halves(pg_url, monkeypatch):
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    monkeypatch.setattr(app, "new_bot", lambda s: Bot(BOT_TOKEN, session=MockedSession()))
    monkeypatch.setattr(app, "WrapperClient", lambda url, key, **kw: wrapper_with(fake))
    await app.run_selfcheck(settings_for(pg_url))
    assert fake.calls("GET", "/me") and fake.calls("GET", "/v1/public/tariffs")


# --- startup isolation ---------------------------------------------------------------------------------------------------------------------


async def test_the_bot_starts_polling_even_when_the_funnel_db_is_down_and_says_so_at_error(monkeypatch, caplog):
    """A dead funnel database must not stop checkout/payment/link/referral from being served: ERROR, not a refusal."""
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()
    session = MockedSession()
    started: list[bool] = []

    async def fake_polling(self, bot, **kw):
        started.append(True)

    monkeypatch.setattr(app, "new_bot", lambda s: Bot(BOT_TOKEN, session=session))
    monkeypatch.setattr(app, "WrapperClient", lambda url, key: wrapper_with(fake))
    monkeypatch.setattr(app.Dispatcher, "start_polling", fake_polling)
    await app.run_bot(make_settings(db_port=1, heartbeat_file="/tmp/ytgen-bot-test.heartbeat"))
    assert started == [True]
    assert any(r.levelno == logging.ERROR and r.getMessage() == "funnel_db_unavailable_at_startup" for r in caplog.records)


async def test_startup_flags_a_database_that_is_behind(pg_url, monkeypatch, caplog):
    empty = await create_database(pg_url, "behind_head")
    fake = FakeWrapper()
    fake.tariffs = funnel_tariffs()

    async def fake_polling(self, bot, **kw):
        return None

    monkeypatch.setattr(app, "new_bot", lambda s: Bot(BOT_TOKEN, session=MockedSession()))
    monkeypatch.setattr(app, "WrapperClient", lambda url, key: wrapper_with(fake))
    monkeypatch.setattr(app.Dispatcher, "start_polling", fake_polling)
    await app.run_bot(settings_for(empty, heartbeat_file="/tmp/ytgen-bot-test.heartbeat"))
    assert any(r.levelno == logging.ERROR and r.getMessage() == "funnel_db_not_at_head" for r in caplog.records)


# --- CLIs ---------------------------------------------------------------------------------------------------------------------------------------


def env_for(url: URL) -> dict[str, str]:
    s = settings_for(url)
    return {
        "BOT_TOKEN": BOT_TOKEN, "BOT_SERVICE_API_KEY": SERVICE_KEY, "WRAPPER_BASE_URL": "https://wrapper.example.test",
        "SUPPORT_CONTACT": s.support_contact, "POSTGRES_HOST": url.host, "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.database, "POSTGRES_USER": url.username, "BOT_DB_PASSWORD": url.password,
        "KIE_API_KEY": KIE_KEY, "KIE_API_URL": "https://api.kie.example", "LLM_MODEL": "m",
        "MANAGER_CHAT_ID": str(MANAGER),
    }


def test_migrate_command_is_idempotent_and_reports_head(pg_url, monkeypatch, capsys):
    fresh = asyncio.run(create_database(pg_url, "cli_migrate"))
    for key, value in env_for(fresh).items():
        monkeypatch.setenv(key, value)
    assert main(["migrate"]) == 0 and main(["migrate"]) == 0
    assert "migrations at head (0001)" in capsys.readouterr().out
    engine = make_engine(fresh)

    async def tables() -> set[str]:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
        await engine.dispose()
        return names

    assert {"funnel_leads", "funnel_events", "alembic_version"} <= asyncio.run(tables())


def test_unknown_command_and_missing_config_exit_with_two(monkeypatch, capsys):
    assert main(["frobnicate"]) == 2
    for key in list(os.environ):
        if key in ("BOT_TOKEN", "KIE_API_KEY", "MANAGER_CHAT_ID", "BOT_DB_PASSWORD"):
            monkeypatch.delenv(key)
    assert main(["funnel-report"]) == 2
    assert "KIE_API_KEY" in capsys.readouterr().err  # names, never values


async def test_funnel_report_prints_the_pdf_section_12_metrics(pg_url, monkeypatch, capsys, store):
    await store.upsert_lead(1, username="a", first_name="A", source="yt")
    await store.upsert_lead(2, username="b", first_name="B", source=None)
    await store.update_lead(1, status="in_spin", step="s2")
    await store.update_lead(2, status="recommended", handoff_reason="payment_method", paid_at=store._clock())
    for event, uid in (("spin_done", 2), ("recommended", 2), ("link_sent", 2)):
        await store.add_event(uid, event)
    await store.add_objection(2, "obj_price")
    for key, value in env_for(pg_url).items():
        monkeypatch.setenv(key, value)
    assert await asyncio.to_thread(main, ["funnel-report"]) == 0
    out = capsys.readouterr().out
    assert "Старт -> конец SPIN: 1 из 2 (50%)" in out
    assert "s2: 1" in out and "Рекомендация -> ссылка на оплату: 1 из 1 (100%)" in out
    assert "obj_price: 1 (1 дошли до link_sent)" in out and "payment_method: 1" in out
    assert "yt: 0 из 1 (0%)" in out and "none: 1 из 1 (100%)" in out


def test_report_with_no_data_does_not_divide_by_zero():
    text = cli.format_report({"started": 0, "spin_done": 0, "dropoff": [], "recommended": 0, "link_sent": 0,
                              "objections": [], "handoff_reasons": [], "sources": []})
    assert "0 из 0 (-)" in text


async def test_ai_sample_prints_a_random_sample_of_the_log(pg_url, monkeypatch, capsys, store):
    await store.upsert_lead(5, username=None, first_name=None, source=None)
    for i in range(6):
        await store.log_ai(5, step="s2", question=f"вопрос {i}", reply=f"ответ {i}", objection_code=None,
                           handoff=False, reason="r", model="m", latency_ms=10 + i, outcome="ok")
    for key, value in env_for(pg_url).items():
        monkeypatch.setenv(key, value)
    assert await asyncio.to_thread(main, ["ai-sample", "--n", "4"]) == 0
    out = capsys.readouterr().out
    assert out.count("Q: вопрос") == 4 and "up to 4" in out and "outcome=ok" in out


def test_ai_replay_and_ai_sample_need_the_full_environment(monkeypatch, capsys):
    monkeypatch.delenv("KIE_API_KEY", raising=False)
    assert main(["ai-replay"]) == 2 and "KIE_API_KEY" in capsys.readouterr().err
