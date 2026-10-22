"""Alembic environment: runs inside a connection handed over by `ytgen_bot.db.migrate` (no URL, no password here)."""
from __future__ import annotations

from alembic import context

config = context.config
connection = config.attributes.get("connection")
if connection is None:  # pragma: no cover - only reachable by running the alembic CLI directly
    raise RuntimeError("run migrations through `python -m ytgen_bot migrate` (it supplies the connection)")

context.configure(connection=connection, target_metadata=None)
with context.begin_transaction():
    context.run_migrations()
