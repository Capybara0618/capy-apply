"""apply v4 schema

Revision ID: 20260524_0001
Revises:
Create Date: 2026-05-24
"""

from __future__ import annotations

from capybot.apply.postgres import metadata

revision = "20260524_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    metadata.create_all(op.get_bind())


def downgrade() -> None:
    from alembic import op

    metadata.drop_all(op.get_bind())
