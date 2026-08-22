"""add active job idempotency

Revision ID: 20260713_0004
Revises: 20260713_0003
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713_0004"
down_revision = "20260713_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("apply_jobs")}
    if "idempotency_key" not in columns:
        op.add_column("apply_jobs", sa.Column("idempotency_key", sa.String(), nullable=True))

    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("apply_jobs")}
    if "uq_apply_jobs_active_key" not in indexes:
        op.create_index(
            "uq_apply_jobs_active_key",
            "apply_jobs",
            ["idempotency_key"],
            unique=True,
            postgresql_where=sa.text(
                "idempotency_key IS NOT NULL AND status IN ('queued', 'running')"
            ),
        )


def downgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("apply_jobs")}
    if "uq_apply_jobs_active_key" in indexes:
        op.drop_index("uq_apply_jobs_active_key", table_name="apply_jobs")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("apply_jobs")}
    if "idempotency_key" in columns:
        op.drop_column("apply_jobs", "idempotency_key")
