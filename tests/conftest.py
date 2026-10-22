from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from aiogram import Bot, Dispatcher

from tests.clock import FakeClock
from tests.fake_wrapper import SERVICE_KEY, FakeWrapper
from tests.mocked_telegram import (
    BOT_TOKEN,
    BOT_USERNAME,
    MockedSession,
    callback_update,
    message_update,
)
from tests.pg import ephemeral_postgres
from ytgen_bot.app import build_dispatcher
from ytgen_bot.config import Settings
from ytgen_bot.context import AppContext
from ytgen_bot.db.engine import make_engine
from ytgen_bot.db.migrate import upgrade_to_head
from ytgen_bot.db.store import PgStore
from ytgen_bot.payments import PaymentWatchers
from ytgen_bot.wrapper import WrapperClient

SUPPORT = "@ytgen_support_test"
UID = 1001


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any real outbound connection is a test failure, even when the caller swallows the exception."""
    attempts: list[object] = []
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if self.family == socket.AF_UNIX or host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)  # type: ignore[arg-type]
        attempts.append(address)
        raise AssertionError(f"real network connection attempted: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
    assert not attempts, f"real network connections were attempted: {attempts}"


@dataclass
class Harness:
    fake: FakeWrapper
    ctx: AppContext
    bot: Bot
    dp: Dispatcher
    session: MockedSession
    counter: int = 0

    async def say(self, text: str, uid: int = UID, **kw: object) -> None:
        self.counter += 1
        await self.dp.feed_update(self.bot, message_update(uid, text, self.counter, **kw))  # type: ignore[arg-type]

    async def press(self, data: str, uid: int = UID) -> None:
        self.counter += 1
        await self.dp.feed_update(self.bot, callback_update(uid, data, self.counter))

    @property
    def texts(self) -> list[str]:
        return self.session.texts()

    @property
    def last(self) -> str:
        return self.session.last_text()


MANAGER = 777000111
KIE_KEY = "kie-test-key-0123456789"


def make_settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "bot_token": BOT_TOKEN, "service_key": SERVICE_KEY, "wrapper_base_url": "https://wrapper.example.test",
        "support_contact": SUPPORT, "log_level": "INFO", "heartbeat_file": "/nonexistent/heartbeat",
        "db_host": "127.0.0.1", "db_port": 1, "db_name": "funnel_test", "db_user": "funnel_test",
        "db_password": "pw-test-0123", "kie_api_key": KIE_KEY, "kie_api_url": "https://kie.example.test",
        "llm_model": "test-model-openai", "manager_chat_id": MANAGER,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def pg_url() -> Iterator[object]:
    """One throw-away Postgres for the whole run, migrated to head through the real Alembic path."""
    with ephemeral_postgres() as url:
        engine = make_engine(url)

        async def migrate() -> None:
            try:
                await upgrade_to_head(engine)
            finally:
                await engine.dispose()

        asyncio.run(migrate())
        yield url


_TABLES = "funnel_leads, funnel_events, funnel_objections, funnel_ai_log, funnel_messages, funnel_followups"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(pg_url: object, clock: FakeClock) -> AsyncIterator[PgStore]:
    import sqlalchemy as sa

    engine = make_engine(pg_url)  # type: ignore[arg-type]
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE {_TABLES} RESTART IDENTITY"))
    yield PgStore(engine, clock=clock)
    await engine.dispose()


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    fake = FakeWrapper()
    wrapper = WrapperClient("https://wrapper.example.test", SERVICE_KEY, transport=fake.transport())
    session = MockedSession()
    bot = Bot(BOT_TOKEN, session=session)
    ctx = AppContext(settings=make_settings(), wrapper=wrapper, bot_username=BOT_USERNAME)
    instant = _instant_sleep()
    watchers_ref = ctx.watchers = PaymentWatchers(ctx, bot, sleep=instant, delays=lambda: iter([1.0, 1.0, 1.0]))
    dp = build_dispatcher(ctx)
    harness = Harness(fake, ctx, bot, dp, session)
    yield harness
    watchers = ctx.watchers or watchers_ref
    await watchers.shutdown()
    await wrapper.aclose()


def _instant_sleep():
    import asyncio

    async def sleep(_: float) -> None:
        await asyncio.sleep(0)

    return sleep


@pytest.fixture
def consented(h: Harness) -> Harness:
    """UID has consented (server-side state, exactly what a returning user has)."""
    h.fake.consent(UID)
    return h


@pytest.fixture
def linked(consented: Harness) -> Harness:
    consented.fake.link(UID, "john@example.com")
    return consented


@pytest.fixture(autouse=True)
def _quiet_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
