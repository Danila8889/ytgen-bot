"""Startup self-check, command list, entry point, heartbeat, and the catch-all error handler."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from tests.conftest import Harness, make_settings
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


# --- the deploy self-check tolerates transient network loss, never anything else ------------------------------------------------


def pieces_with_session(key: str = SERVICE_KEY):
    fake = FakeWrapper()
    wrapper = WrapperClient("https://wrapper.example.test", key, transport=fake.transport())
    session = MockedSession()
    return fake, wrapper, session, Bot(BOT_TOKEN, session=session)


def timeout_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message="Request timeout error")


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(s: float) -> None:
        recorded.append(s)

    monkeypatch.setattr(app.asyncio, "sleep", fake_sleep)
    return recorded


def retry_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage() == "self_check_retry"]


async def test_telegram_timeouts_then_success_passes_and_every_retry_is_a_warning(sleeps, caplog):
    _, wrapper, session, bot = pieces_with_session()
    session.get_me_errors.extend([timeout_error(), timeout_error()])
    delays = (1.0, 2.0, 3.0)
    name = await app.self_check(bot, wrapper, retry_delays=(), telegram_retry_delays=delays, request_timeout_s=10)
    assert name == BOT_USERNAME
    assert sleeps == [1.0, 2.0] and session.timeouts == [10, 10, 10]
    warnings = retry_warnings(caplog)
    assert [(w.attempt, w.attempts, w.error) for w in warnings] == [(1, 4, "TelegramNetworkError"), (2, 4, "TelegramNetworkError")]


async def test_persistent_telegram_timeout_fails_after_all_attempts_with_a_clear_message(sleeps, caplog):
    fake, wrapper, session, bot = pieces_with_session()
    session.get_me_errors.extend(timeout_error() for _ in range(10))
    with pytest.raises(app.SelfCheckError) as err:
        await app.self_check(bot, wrapper, retry_delays=(), telegram_retry_delays=(1.0, 2.0, 3.0, 4.0, 5.0))
    message = str(err.value)
    assert "Telegram getMe unreachable after 6 attempts" in message and "TelegramNetworkError" in message
    assert BOT_TOKEN not in message and len(session.requests) == 6 and sleeps == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(retry_warnings(caplog)) == 5 and not fake.requests  # the wrapper is never reached


async def test_telegram_401_fails_on_the_first_attempt_without_retries(sleeps, caplog):
    fake, wrapper, session, bot = pieces_with_session()
    session.get_me_errors.append(TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"))
    with pytest.raises(app.SelfCheckError, match="rejected BOT_TOKEN") as err:
        await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0), telegram_retry_delays=(1.0, 2.0))
    assert BOT_TOKEN not in str(err.value)
    assert len(session.requests) == 1 and sleeps == [] and not retry_warnings(caplog) and not fake.requests


async def test_wrapper_flake_is_retried_with_warnings_then_passes(sleeps, caplog):
    fake, wrapper, _, bot = pieces_with_session()
    fake.queue("GET", "/me", httpx.ConnectTimeout("slow"))
    fake.queue("GET", "/me", httpx.ConnectError("reset"))
    assert await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0, 3.0)) == BOT_USERNAME
    assert sleeps == [1.0, 2.0] and len(fake.calls("GET", "/me")) == 3
    assert [w.error for w in retry_warnings(caplog)] == ["WrapperUnavailable <- ConnectTimeout", "WrapperUnavailable <- ConnectError"]


async def test_persistent_wrapper_flake_fails_after_all_attempts(sleeps):
    fake, wrapper, _, bot = pieces_with_session()
    for _ in range(6):
        fake.queue("GET", "/me", httpx.ConnectTimeout("slow"))
    with pytest.raises(app.SelfCheckError, match=r"wrapper \(WRAPPER_BASE_URL\) unreachable after 6 attempts"):
        await app.self_check(bot, wrapper, retry_delays=(1.0,) * 5)
    assert len(fake.calls("GET", "/me")) == 6


@pytest.mark.parametrize("status,code", [(401, "invalid_bot_service_key"), (403, "forbidden"), (503, "bot_api_disabled")])
async def test_wrapper_auth_and_status_errors_are_not_retried(sleeps, caplog, status, code):
    fake, wrapper, _, bot = pieces_with_session()
    fake.queue_error("GET", "/me", status, code)
    with pytest.raises(app.SelfCheckError):
        await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0), telegram_retry_delays=(1.0, 2.0))
    assert len(fake.calls("GET", "/me")) == 1 and sleeps == [] and not retry_warnings(caplog)


async def test_a_wrapper_contract_violation_is_not_retried(sleeps, caplog):
    from ytgen_bot.wrapper import WrapperContractError

    fake, wrapper, _, bot = pieces_with_session()
    fake.queue("GET", "/me", httpx.Response(200, json={"oops": True}))
    with pytest.raises(WrapperContractError):
        await app.self_check(bot, wrapper, retry_delays=(1.0, 2.0))
    assert len(fake.calls("GET", "/me")) == 1 and sleeps == [] and not retry_warnings(caplog)


async def test_the_runtime_startup_check_still_makes_one_telegram_attempt():
    """`run_bot` calls `self_check(bot, wrapper)`: no Telegram retries and no custom timeout there."""
    _, wrapper, session, bot = pieces_with_session()
    session.get_me_errors.append(timeout_error())
    with pytest.raises(app.SelfCheckError, match="after 1 attempt "):
        await app.self_check(bot, wrapper)
    assert len(session.requests) == 1 and session.timeouts == [None]


async def test_run_selfcheck_uses_the_short_timeout_and_the_network_retry_policy(monkeypatch, sleeps):
    session = MockedSession()
    session.get_me_errors.extend(timeout_error() for _ in range(20))
    built: dict[str, object] = {}

    def make_wrapper(url, key, **kw):
        built.update(kw)
        return WrapperClient(url, key, **kw, transport=FakeWrapper().transport())

    monkeypatch.setattr(app, "new_bot", lambda s: Bot(BOT_TOKEN, session=session))
    monkeypatch.setattr(app, "WrapperClient", make_wrapper)
    with pytest.raises(app.SelfCheckError, match="after 6 attempts"):
        await app.run_selfcheck(make_settings())
    assert built == {"timeout_s": app.SELFCHECK_REQUEST_TIMEOUT_S}
    assert set(session.timeouts) == {app.SELFCHECK_REQUEST_TIMEOUT_S} and len(session.timeouts) == 6
    assert sleeps == list(app.SELFCHECK_NETWORK_RETRY_DELAYS_S)
    # worst case per call stays inside the overall deadline
    budget = 6 * app.SELFCHECK_REQUEST_TIMEOUT_S + sum(app.SELFCHECK_NETWORK_RETRY_DELAYS_S)
    assert budget < app.SELFCHECK_DEADLINE_S <= 300


async def test_run_selfcheck_has_a_hard_overall_deadline(monkeypatch):
    class Hanging(MockedSession):
        async def make_request(self, bot, method, timeout=None):
            await asyncio.sleep(30)

    monkeypatch.setattr(app, "new_bot", lambda s: Bot(BOT_TOKEN, session=Hanging()))
    monkeypatch.setattr(app, "WrapperClient", lambda url, key, **kw: WrapperClient(url, key, transport=FakeWrapper().transport()))
    monkeypatch.setattr(app, "SELFCHECK_DEADLINE_S", 0.05)
    with pytest.raises(app.SelfCheckError, match="did not finish within"):
        await app.run_selfcheck(make_settings())


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
