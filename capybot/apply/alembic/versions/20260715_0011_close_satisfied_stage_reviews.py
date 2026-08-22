"""Close pending stage suggestions already reflected by the opportunity.

Revision ID: 20260715_0011
Revises: 20260715_0010
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0011"
down_revision = "20260715_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if "review_items" not in set(sa.inspect(bind).get_table_names()):
        return
    bind.execute(
        sa.text("""
        DELETE FROM review_items r
        USING opportunities o
        WHERE r.opportunity_id=o.id
          AND r.kind='stage'
          AND r.status='pending'
          AND r.payload::jsonb->>'stage'=o.stage
    """)
    )


def downgrade() -> None:
    pass
