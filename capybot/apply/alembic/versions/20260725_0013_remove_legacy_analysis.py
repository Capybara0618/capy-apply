"""Remove obsolete analysis paths after the first opportunity Agent cutover.

Revision ID: 20260725_0013
Revises: 20260725_0012
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0013"
down_revision = "20260725_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM agent_runs
            WHERE COALESCE(engine, '') <> 'opportunity_harness_v1'
            """
        )
    )
    bind.execute(sa.text("DELETE FROM apply_jobs WHERE job_type='analyze_job_fit'"))
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "analysis_runs" in tables:
        op.drop_table("analysis_runs")
    opportunity_columns = {
        item["name"] for item in inspector.get_columns("opportunities")
    }
    if "fit_score" in opportunity_columns:
        op.drop_column("opportunities", "fit_score")
    run_columns = {item["name"] for item in inspector.get_columns("agent_runs")}
    if "rebuilt_from_legacy" in run_columns:
        op.drop_column("agent_runs", "rebuilt_from_legacy")


def downgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("rebuilt_from_legacy", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("opportunities", sa.Column("fit_score", sa.Integer()))
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("boss_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False, server_default="{}"),
    )
