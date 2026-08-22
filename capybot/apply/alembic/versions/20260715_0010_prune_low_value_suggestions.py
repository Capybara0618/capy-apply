"""Prune unconfirmed actions that contradict the opportunity stage.

Revision ID: 20260715_0010
Revises: 20260715_0009
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0010"
down_revision = "20260715_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    tables = set(sa.inspect(bind).get_table_names())
    if "review_items" in tables:
        bind.execute(
        sa.text("""
        DELETE FROM review_items r
        USING opportunities o
        WHERE r.opportunity_id=o.id
          AND r.status='pending'
          AND (
            (r.kind='draft' AND o.stage NOT IN ('need_my_action', 'interviewing'))
            OR (r.kind='task' AND o.stage IN ('discovered', 'communicating', 'closed'))
          )
    """)
        )
    if "reply_drafts" in tables:
        bind.execute(
        sa.text("""
        DELETE FROM reply_drafts d
        USING opportunities o
        WHERE d.opportunity_id=o.id
          AND d.status='pending'
          AND o.stage NOT IN ('need_my_action', 'interviewing')
    """)
        )
    if "followup_tasks" in tables:
        bind.execute(
        sa.text("""
        DELETE FROM followup_tasks t
        USING opportunities o
        WHERE t.opportunity_id=o.id
          AND t.status='suggested'
          AND o.stage IN ('discovered', 'communicating', 'closed')
    """)
        )


def downgrade() -> None:
    # Derived suggestions require an Agent rerun and cannot be recreated here.
    pass
