"""add agent run performance metrics

Revision ID: 20260713_0003
Revises: 20260713_0002
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713_0003"
down_revision = "20260713_0002"
branch_labels = None
depends_on = None


METRIC_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("llm_call_count", sa.Integer(), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), nullable=True),
    sa.Column("completion_tokens", sa.Integer(), nullable=True),
    sa.Column("duration_ms", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("agent_runs")}
    for column in METRIC_COLUMNS:
        if column.name not in columns:
            op.add_column("agent_runs", column)

    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("agent_runs")}
    if "ix_agent_runs_account_started" not in indexes:
        op.create_index(
            "ix_agent_runs_account_started",
            "agent_runs",
            ["account_id", "started_at"],
        )


def downgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("agent_runs")}
    if "ix_agent_runs_account_started" in indexes:
        op.drop_index("ix_agent_runs_account_started", table_name="agent_runs")

    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_runs")}
    for column in reversed(METRIC_COLUMNS):
        if column.name in columns:
            op.drop_column("agent_runs", column.name)
