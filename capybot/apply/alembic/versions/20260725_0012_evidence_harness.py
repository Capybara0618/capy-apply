"""Add the evidence ledger used by the independent opportunity Agent.

Revision ID: 20260725_0012
Revises: 20260715_0011
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0012"
down_revision = "20260715_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("opportunities")}
    if "pursuit_recommendation" not in columns:
        op.add_column(
            "opportunities",
            sa.Column("pursuit_recommendation", sa.String()),
        )
    if "open_questions" not in columns:
        op.add_column(
            "opportunities",
            sa.Column("open_questions", sa.Text(), nullable=False, server_default="[]"),
        )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "boss_job_snapshots" not in tables:
        _create_job_snapshots()
    if "research_sources" not in tables:
        _create_research_sources()
    if "sync_checkpoints" not in tables:
        _create_sync_checkpoints()
    if "tool_observations" not in tables:
        _create_tool_observations()


def _create_job_snapshots() -> None:
    op.create_table(
        "boss_job_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("apply_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            sa.String(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("boss_conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("platform_job_id", sa.String()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("captured_at", sa.String(), nullable=False),
        sa.UniqueConstraint(
            "opportunity_id",
            "version",
            name="uq_boss_job_snapshots_version",
        ),
    )
    op.create_index(
        "ix_boss_job_snapshots_opportunity_captured",
        "boss_job_snapshots",
        ["opportunity_id", "captured_at"],
    )


def _create_research_sources() -> None:
    op.create_table(
        "research_sources",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("apply_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            sa.String(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("source_domain", sa.String()),
        sa.Column("excerpt", sa.Text()),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("retrieved_at", sa.String(), nullable=False),
        sa.UniqueConstraint(
            "opportunity_id",
            "content_hash",
            name="uq_research_sources_content",
        ),
    )
    op.create_index(
        "ix_research_sources_opportunity_retrieved",
        "research_sources",
        ["opportunity_id", "retrieved_at"],
    )


def _create_sync_checkpoints() -> None:
    op.create_table(
        "sync_checkpoints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(),
            sa.ForeignKey("apply_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(),
            sa.ForeignKey("boss_conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("cursor", sa.Text()),
        sa.Column("last_message_id", sa.String()),
        sa.Column("snapshot_hash", sa.String()),
        sa.Column("synced_at", sa.String(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "conversation_id",
            "source",
            name="uq_sync_checkpoint_scope",
        ),
    )


def _create_tool_observations() -> None:
    op.create_table(
        "tool_observations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("server_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("arguments_summary", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_summary", sa.Text()),
        sa.Column("evidence_refs", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_tool_observations_call"),
    )
    op.create_index(
        "ix_tool_observations_run_created",
        "tool_observations",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("tool_observations")
    op.drop_table("sync_checkpoints")
    op.drop_table("research_sources")
    op.drop_table("boss_job_snapshots")
    op.drop_column("opportunities", "open_questions")
    op.drop_column("opportunities", "pursuit_recommendation")
