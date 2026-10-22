"""Engine construction. The password only ever lives in the URL object (never rendered to a string)."""
from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..config import Settings

CONNECT_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 15.0


def build_url(settings: Settings) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def make_engine(url: URL) -> AsyncEngine:
    """Small pool, pre-ping (a Postgres restart must not leave dead connections), bounded connect/command time so a
    sick database fails fast instead of hanging a handler."""
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"timeout": CONNECT_TIMEOUT_S, "command_timeout": COMMAND_TIMEOUT_S},
    )
