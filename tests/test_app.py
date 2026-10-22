"""Startup self-check, command list, entry point, heartbeat, and the catch-all error handler."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from aiogram import Bot

from tests.conftest import Harness
from tests.fake_wrapper import SERVICE_KEY, FakeWrapper
from tests.mocked_telegram import BOT_ID, BOT_TOKEN, BOT_USERNAME, MockedSession
from ytgen_bot import app, texts
from ytgen_bot.__main__ import main
from ytgen_bot.wrapper import WrapperClient


def pieces(key: str = SERVICE_KEY):
    fake = FakeWrapper()
    wrapper = WrapperClient("https://wrapper.example.test", key, transport=fake.transport())
    session = MockedSession()
    return fake, wrapper, Bot(BOT_TOKEN, session=session)


async def test_self_check_ok_uses_the_bots_own_id_and_returns_the_username():
    fake, wrapper, bot = pieces()
    assert await app.self_check(bot, wrapper, retry_delays=()) == BOT_USERNAME
    call = fake.requests[0]
    assert (call["method"], call["path"]) == ("GET", "/me")
    assert call["headers"]["x-telegram-user-id"] == str(BOT_ID)


async def test_self_check_wrong_key_says_it_must_equal_the_wrappers():
    _, wrapper, bot = pieces(key="x" * 40)
    with pytest.raises(app.SelfCheckError, match="must equal the wrapper's"):
        await app.self_check(bot, wrapper, retry_delays=())


async def test_self_check_disabled_wrapper_is_named():
    fake, wrapper, bot = pieces()
    fake.queue_error("GET", "/me", 503, "bot_api_disabled", reason="BOT_SERVICE_API_KEY is not configured")
    with pytest.raises(app.SelfCheckError, match="disabled"):
        await app.self_check(bot, wrapper, retry_delays=())


async def test_self_check_retries_a_restarting_wrapper_then_gives_up(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    fake, wrapper, bot = pieces()
    for _ in range(3):
        fake.queue("GET", "/me", httpx.ConnectError("down"))
    assert await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0, 3.0)) == BOT_USERNAME  # 4th call succeeds
    assert sleeps == [1.0, 2.0, 3.0]
    for _ in range(3):
        fake.queue("GET", "/me", httpx.ConnectError("down"))
    with pytest.raises(app.SelfCheckError, match="unreachable"):
        await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0))


async def test_self_check_unexpected_status_is_named():
    fake, wrapper, bot = pieces()
    fake.queue_error("GET", "/me", 400, "invalid_telegram_user_id")
    with pytest.raises(app.SelfCheckError, match="400 invalid_telegram_user_id"):
        await app.self_check(bot, wrapper, retry_delays=())


def test_commands_list_matches_the_spec():
    assert [c.command for c in app.COMMANDS] == ["start", "tariffs", "referral", "status", "stop", "help"]
    assert all(c.description for c in app.COMMANDS)


async def test_unhandled_exception_is_logged_and_the_user_gets_a_generic_message(linked: Harness, caplog, monkeypatch):
    from ytgen_bot.handlers import menu

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(menu, "show_help", boom)
    await linked.say("/help")
    assert linked.last == texts.GENERIC_ERROR
    assert any(r.message == "unhandled_error" and r.levelno == logging.ERROR for r in caplog.records)


async def test_a_wrapper_contract_failure_never_looks_like_success(linked: Harness):
    linked.fake.queue("GET", "/tariffs", httpx.Response(200, json={"oops": True}))
    await linked.say("/tariffs")
    assert linked.last == texts.GENERIC_ERROR


async def test_heartbeat_writes_only_while_telegram_answers(tmp_path):
    _, _, bot = pieces()
    path = tmp_path / "hb"
    task = asyncio.create_task(app._heartbeat(bot, str(path)))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert path.read_text().isdigit()


def test_main_rejects_missing_config_and_names_the_variables(monkeypatch, capsys):
    for name in ("BOT_TOKEN", "BOT_SERVICE_API_KEY", "WRAPPER_BASE_URL", "SUPPORT_CONTACT"):
        monkeypatch.delenv(name, raising=False)
    assert main(["run"]) == 2
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err and "BOT_SERVICE_API_KEY" in err and "WRAPPER_BASE_URL" in err and "SUPPORT_CONTACT" in err


def test_main_unknown_command(capsys):
    assert main(["bogus"]) == 2
