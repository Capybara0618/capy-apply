"""Add runtime integrity constraints and hot-path indexes.

Revision ID: 20260715_0009
Revises: 20260715_0008
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0009"
down_revision = "20260715_0008"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_boss_job_cards_conversation", "boss_job_cards", ("conversation_id",), False),
    (
        "ix_opportunities_account_stage_updated",
        "opportunities",
        ("account_id", "stage", "updated_at"),
        False,
    ),
    (
        "ix_opportunities_account_priority",
        "opportunities",
        ("account_id", "opportunity_priority_score"),
        False,
    ),
    (
        "ix_conversation_opportunities_opportunity",
        "conversation_opportunities",
        ("opportunity_id",),
        False,
    ),
    (
        "ix_apply_events_opportunity_created",
        "apply_events",
        ("opportunity_id", "created_at"),
        False,
    ),
    ("ix_followup_tasks_opportunity_status", "followup_tasks", ("opportunity_id", "status"), False),
    ("ix_followup_tasks_status_due", "followup_tasks", ("status", "due_at"), False),
    (
        "ix_reply_drafts_opportunity_updated",
        "reply_drafts",
        ("opportunity_id", "updated_at"),
        False,
    ),
    ("ix_review_items_opportunity_status", "review_items", ("opportunity_id", "status"), False),
    (
        "ix_fit_results_opportunity_updated",
        "opportunity_fit_results",
        ("opportunity_id", "updated_at"),
        False,
    ),
    ("ix_agent_runs_opportunity_started", "agent_runs", ("opportunity_id", "started_at"), False),
    ("uq_agent_trace_run_step", "agent_trace_steps", ("run_id", "step_index"), True),
    ("ix_import_items_run_mode", "import_run_items", ("import_run_id", "analysis_mode"), False),
    ("ix_apply_jobs_account_created", "apply_jobs", ("account_id", "created_at"), False),
    ("ix_apply_jobs_status_updated", "apply_jobs", ("status", "updated_at"), False),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    bind.execute(
        sa.text(
            "UPDATE agent_runs SET conversation_id=NULL WHERE conversation_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM boss_conversations c WHERE c.id=agent_runs.conversation_id)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE agent_runs SET opportunity_id=NULL WHERE opportunity_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id=agent_runs.opportunity_id)"
        )
    )
    _create_fk("agent_runs", "conversation_id", "boss_conversations", "SET NULL")
    _create_fk("agent_runs", "opportunity_id", "opportunities", "SET NULL")

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, columns, unique in INDEXES:
        if table not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, list(columns), unique=unique)


def _create_fk(table: str, column: str, parent: str, ondelete: str) -> None:
    bind = op.get_bind()
    if any(
        item.get("constrained_columns") == [column] and item.get("referred_table") == parent
        for item in sa.inspect(bind).get_foreign_keys(table)
    ):
        return
    op.create_foreign_key(
        f"fk_{table}_{column}", table, parent, [column], ["id"], ondelete=ondelete
    )


def downgrade() -> None:
    for name, table, _columns, _unique in reversed(INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_constraint("fk_agent_runs_opportunity_id", "agent_runs", type_="foreignkey")
    op.drop_constraint("fk_agent_runs_conversation_id", "agent_runs", type_="foreignkey")
