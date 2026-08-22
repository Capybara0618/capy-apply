"""label job-fit runs with their actual Agent engine

Revision ID: 20260714_0006
Revises: 20260713_0005
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260714_0006"
down_revision = "20260713_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text("""
        UPDATE agent_runs
        SET engine = 'job_fit_agent',
            planner_mode = COALESCE(planner_mode, 'single_llm_fit')
        WHERE target_type = 'job_fit'
          AND (engine IS NULL OR engine IN ('legacy', 'legacy_workflow'))
    """)
    )


def downgrade() -> None:
    # Historical runs remain correctly labeled if the schema is downgraded.
    pass
