"""Apply / inspect Alembic migrations programmatically (no alembic.ini, no URL on disk).

`upgrade_to_head` is idempotent (a database already at head is a no-op), serialised with a Postgres advisory lock
(two deploys cannot interleave) and fails loudly: any error propagates and the one-shot `migrate` compose service
exits non-zero, which stops the deploy before the bot starts.
"""
from __future__ import annotations

import logging
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_ADVISORY_LOCK_KEY = 7_203_411_901  # arbitrary, only needs to be unique to this application


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:  # pragma: no cover - the repo always ships at least 0001
        raise RuntimeError("no Alembic head revision found")
    return head


def _upgrade(connection: sa.Connection) -> None:
    connection.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
    cfg = _config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def upgrade_to_head(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
    log.info("migrations_applied", extra={"revision": head_revision()})


def _current(connection: sa.Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(_current)


async def at_head(engine: AsyncEngine) -> bool:
    return await current_revision(engine) == head_revision()
