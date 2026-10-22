"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

ADDITIVE ONLY (new tables / nullable-or-defaulted columns / indexes): a rollback to the previous image must run
on the migrated schema. See documentation.md "Database".
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}
