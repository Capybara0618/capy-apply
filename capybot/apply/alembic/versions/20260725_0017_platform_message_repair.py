"""repair known BOSS platform UI messages

Revision ID: 20260725_0017
Revises: 20260725_0016
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op

revision = "20260725_0017"
down_revision = "20260725_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE boss_messages
        SET message_type='platform_card', is_human_message=0
        WHERE text LIKE '%点击修改打招呼语%'
           OR text LIKE '%去修改打招呼语%'
        """
    )


def downgrade() -> None:
    # The original raw payload does not prove that these UI prompts were human text.
    pass
