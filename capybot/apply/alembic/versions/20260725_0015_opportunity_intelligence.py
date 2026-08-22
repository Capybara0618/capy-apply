"""add quality metadata for opportunity intelligence evidence

Revision ID: 20260725_0015
Revises: 20260725_0014
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0015"
down_revision = "20260725_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("research_sources")}
    additions = (
        ("research_type", sa.String(), "legacy"),
        ("source_tier", sa.String(), "general"),
        ("quality_score", sa.Float(), "0.4"),
        ("verified", sa.Boolean(), "false"),
        ("published_at", sa.String(), None),
        ("metadata", sa.Text(), "{}"),
        ("last_checked_at", sa.String(), None),
    )
    for name, column_type, default in additions:
        if name in columns:
            continue
        kwargs: dict[str, object] = {}
        if default is not None:
            kwargs.update(nullable=False, server_default=default)
        op.add_column("research_sources", sa.Column(name, column_type, **kwargs))

    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("research_sources")
    }
    if "ix_research_sources_type_quality" not in indexes:
        op.create_index(
            "ix_research_sources_type_quality",
            "research_sources",
            ["opportunity_id", "research_type", "quality_score"],
        )


def downgrade() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("research_sources")
    }
    if "ix_research_sources_type_quality" in indexes:
        op.drop_index("ix_research_sources_type_quality", table_name="research_sources")
    for name in (
        "last_checked_at",
        "metadata",
        "published_at",
        "verified",
        "quality_score",
        "source_tier",
        "research_type",
    ):
        op.drop_column("research_sources", name)
