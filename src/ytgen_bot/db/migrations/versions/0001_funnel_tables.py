"""funnel tables: leads, events, objections, ai_log, message buffer, follow-up schedule

Revision ID: 0001
Revises:
Create Date: 2026-10-22

ADDITIVE ONLY: a rollback to the previous image must run on the migrated schema (it simply ignores the tables).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "funnel_leads",
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
    op.create_table(
        "funnel_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("tg_id", sa.BigInteger, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=True),
    )
    op.create_table(
        "funnel_objections",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("ts", TZ, nullable=False),
    )
    op.create_table(
        "funnel_ai_log",
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
    op.create_table(
        "funnel_messages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
    )
    op.create_table(
        "funnel_followups",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("due_at", TZ, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("claimed_at", TZ, nullable=True),
        sa.Column("done_at", TZ, nullable=True),
    )
    op.create_index("ix_funnel_events_tg", "funnel_events", ["tg_id"])
    op.create_index("ix_funnel_events_event_ts", "funnel_events", ["event", "ts"])
    op.create_index("ix_funnel_objections_tg", "funnel_objections", ["tg_id"])
    op.create_index("ix_funnel_ai_log_ts", "funnel_ai_log", ["ts"])
    op.create_index("ix_funnel_ai_log_tg_ts", "funnel_ai_log", ["tg_id", "ts"])
    op.create_index("ix_funnel_messages_tg", "funnel_messages", ["tg_id"])
    op.create_index("ix_funnel_followups_due", "funnel_followups", ["state", "due_at"])
    op.create_index(
        "uq_funnel_followups_one_pending",
        "funnel_followups",
        ["tg_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
