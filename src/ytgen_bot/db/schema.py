"""Table definitions (SQLAlchemy Core). The Alembic migrations are written by hand and a test pins that
`alembic upgrade head` produces exactly these tables and columns.

Migration rule (also in documentation.md): ADDITIVE ONLY - new tables, new nullable/defaulted columns, new
indexes. Never drop, rename or retype in a migration: a rollback to the previous image must run unchanged on the
migrated schema.
"""
from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

TZ = sa.DateTime(timezone=True)

leads = sa.Table(
    "funnel_leads",
    metadata,
    sa.Column("tg_id", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("username", sa.Text, nullable=True),
    sa.Column("first_name", sa.Text, nullable=True),
    sa.Column("source", sa.Text, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default="new"),
    sa.Column("step", sa.Text, nullable=True),
    sa.Column("stage", sa.Text, nullable=True),
    sa.Column("volume", sa.Text, nullable=True),
    sa.Column("niche_text", sa.Text, nullable=True),
    sa.Column("pain", sa.Text, nullable=True),
    sa.Column("pain_text", sa.Text, nullable=True),
    sa.Column("impact", sa.Text, nullable=True),
    sa.Column("goal", sa.Text, nullable=True),
    sa.Column("rec_role", sa.Text, nullable=True),
    sa.Column("rec_tariff_id", sa.Integer, nullable=True),
    sa.Column("handoff_reason", sa.Text, nullable=True),
    sa.Column("handoff_at", TZ, nullable=True),
    sa.Column("handoff_priority", sa.Text, nullable=True),
    sa.Column("remind_ok", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("reminders_opt_in", sa.Boolean, nullable=True),
    sa.Column("followups_sent", sa.Integer, nullable=False, server_default="0"),
    sa.Column("answered_followup", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("last_followup_at", TZ, nullable=True),
    sa.Column("unsub", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("paid_tariff", sa.Text, nullable=True),
    sa.Column("paid_at", TZ, nullable=True),
    sa.Column("paid_by_manager", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("paid_announced", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("pay_check_sent", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("notes", sa.Text, nullable=True),
    sa.Column("last_activity_at", TZ, nullable=False),
    sa.Column("created_at", TZ, nullable=False),
    sa.Column("updated_at", TZ, nullable=False),
    sa.Column("stopped_at", TZ, nullable=True),
)

events = sa.Table(
    "funnel_events",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("ts", TZ, nullable=False),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=True),  # a button code or a short token, never free text
)

objections = sa.Table(
    "funnel_objections",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("code", sa.Text, nullable=False),
    sa.Column("ts", TZ, nullable=False),
)

ai_log = sa.Table(
    "funnel_ai_log",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("ts", TZ, nullable=False),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("step", sa.Text, nullable=True),
    sa.Column("question", sa.Text, nullable=False),
    sa.Column("reply", sa.Text, nullable=True),
    sa.Column("objection_code", sa.Text, nullable=True),
    sa.Column("handoff", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column("model", sa.Text, nullable=True),
    sa.Column("latency_ms", sa.Integer, nullable=True),
    sa.Column("outcome", sa.Text, nullable=False),
)

messages = sa.Table(
    "funnel_messages",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("ts", TZ, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
)

followups = sa.Table(
    "funnel_followups",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("tg_id", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("due_at", TZ, nullable=False),
    sa.Column("state", sa.Text, nullable=False, server_default="pending"),
    sa.Column("created_at", TZ, nullable=False),
    sa.Column("claimed_at", TZ, nullable=True),
    sa.Column("done_at", TZ, nullable=True),
)

INDEXES = (
    sa.Index("ix_funnel_events_tg", events.c.tg_id),
    sa.Index("ix_funnel_events_event_ts", events.c.event, events.c.ts),
    sa.Index("ix_funnel_objections_tg", objections.c.tg_id),
    sa.Index("ix_funnel_ai_log_ts", ai_log.c.ts),
    sa.Index("ix_funnel_ai_log_tg_ts", ai_log.c.tg_id, ai_log.c.ts),
    sa.Index("ix_funnel_messages_tg", messages.c.tg_id),
    sa.Index("ix_funnel_followups_due", followups.c.state, followups.c.due_at),
    sa.Index(
        "uq_funnel_followups_one_pending",
        followups.c.tg_id,
        unique=True,
        postgresql_where=followups.c.state == "pending",
    ),
)
