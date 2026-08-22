"""backfill metrics for historical agent runs

Revision ID: 20260713_0005
Revises: 20260713_0004
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713_0005"
down_revision = "20260713_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text("""
        UPDATE agent_runs
        SET duration_ms = GREATEST(
            0,
            ROUND(EXTRACT(EPOCH FROM (
                finished_at::timestamptz - started_at::timestamptz
            )) * 1000)::integer
        )
        WHERE duration_ms IS NULL
          AND started_at IS NOT NULL
          AND finished_at IS NOT NULL
    """)
    )
    bind.execute(
        sa.text("""
        UPDATE agent_runs AS run
        SET llm_call_count = trace.calls
        FROM (
            SELECT run_id, COUNT(*)::integer AS calls
            FROM agent_trace_steps
            WHERE step_type IN ('llm_final', 'llm_extract')
            GROUP BY run_id
        ) AS trace
        WHERE run.id = trace.run_id
          AND run.llm_call_count IS NULL
    """)
    )


def downgrade() -> None:
    # The columns remain valid; historical measurements are intentionally kept.
    pass
