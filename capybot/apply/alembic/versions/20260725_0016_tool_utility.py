"""add measurable utility fields to tool observations

Revision ID: 20260725_0016
Revises: 20260725_0015
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0016"
down_revision = "20260725_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("tool_observations")
    }
    additions = (
        ("fact_count", sa.Integer(), "0"),
        ("novel_evidence_count", sa.Integer(), "0"),
        ("used_evidence_count", sa.Integer(), "0"),
        ("empty_result", sa.Boolean(), "false"),
        ("utility", sa.String(), "unknown"),
    )
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column(
                "tool_observations",
                sa.Column(
                    name,
                    column_type,
                    nullable=False,
                    server_default=default,
                ),
            )


def downgrade() -> None:
    for name in (
        "utility",
        "empty_result",
        "used_evidence_count",
        "novel_evidence_count",
        "fact_count",
    ):
        op.drop_column("tool_observations", name)
