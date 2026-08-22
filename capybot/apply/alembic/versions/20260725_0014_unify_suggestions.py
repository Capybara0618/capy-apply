"""unify task, draft, risk and review suggestions

Revision ID: 20260725_0014
Revises: 20260725_0013
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0014"
down_revision = "20260725_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "suggestions" not in tables:
        op.create_table(
            "suggestions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "conversation_id",
                sa.String(),
                sa.ForeignKey("boss_conversations.id", ondelete="CASCADE"),
            ),
            sa.Column(
                "opportunity_id",
                sa.String(),
                sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="suggested"),
            sa.Column("due_at", sa.String()),
            sa.Column("priority", sa.String()),
            sa.Column("reason", sa.Text()),
            sa.Column("severity", sa.String()),
            sa.Column("evidence_refs", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
        )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "followup_tasks" in tables:
        op.execute(
            """
            INSERT INTO suggestions
              (id, conversation_id, opportunity_id, kind, content, status, due_at,
               priority, reason, severity, evidence_refs, fingerprint, payload,
               created_at, updated_at)
            SELECT id, conversation_id, opportunity_id, 'task', title, status, due_at,
                   priority, reason, NULL, evidence_message_ids,
                   md5(lower(regexp_replace(title, '\\s+', ' ', 'g'))),
                   '{}'::text, created_at, updated_at
            FROM followup_tasks
            ON CONFLICT (id) DO NOTHING
            """
        )
    if "reply_drafts" in tables:
        op.execute(
            """
            INSERT INTO suggestions
              (id, conversation_id, opportunity_id, kind, content, status, due_at,
               priority, reason, severity, evidence_refs, fingerprint, payload,
               created_at, updated_at)
            SELECT id, conversation_id, opportunity_id, 'draft', content,
                   CASE WHEN status='pending' THEN 'suggested' ELSE status END,
                   NULL, NULL, reason, NULL, evidence_message_ids,
                   md5(lower(regexp_replace(content, '\\s+', ' ', 'g'))),
                   '{}'::text, created_at, updated_at
            FROM reply_drafts
            ON CONFLICT (id) DO NOTHING
            """
        )
    if "review_items" in tables:
        op.execute(
            """
            INSERT INTO suggestions
              (id, conversation_id, opportunity_id, kind, content, status, due_at,
               priority, reason, severity, evidence_refs, fingerprint, payload,
               created_at, updated_at)
            SELECT id, conversation_id, opportunity_id, 'stage', title,
                   CASE WHEN status='pending' THEN 'suggested' ELSE status END,
                   NULL, NULL, NULL, NULL, evidence_message_ids,
                   COALESCE(NULLIF(suggestion_key, ''), md5(lower(title))),
                   payload, created_at, updated_at
            FROM review_items
            WHERE kind='stage' AND opportunity_id IS NOT NULL
            ON CONFLICT (id) DO NOTHING
            """
        )

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("suggestions")}
    if "ix_suggestions_opportunity_status" not in indexes:
        op.create_index(
            "ix_suggestions_opportunity_status",
            "suggestions",
            ["opportunity_id", "status"],
        )
    if "ix_suggestions_kind_status_due" not in indexes:
        op.create_index(
            "ix_suggestions_kind_status_due",
            "suggestions",
            ["kind", "status", "due_at"],
        )
    if "uq_suggestions_opportunity_kind_fingerprint" not in indexes:
        op.create_index(
            "uq_suggestions_opportunity_kind_fingerprint",
            "suggestions",
            ["opportunity_id", "kind", "fingerprint"],
            unique=True,
        )

    for table in ("review_items", "reply_drafts", "followup_tasks"):
        if table in set(sa.inspect(op.get_bind()).get_table_names()):
            op.drop_table(table)


def downgrade() -> None:
    op.create_table(
        "followup_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String()),
        sa.Column("opportunity_id", sa.String()),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("due_at", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("evidence_message_ids", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String()),
        sa.Column("opportunity_id", sa.String()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("evidence_message_ids", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "review_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evidence_message_ids", sa.Text(), nullable=False),
        sa.Column("opportunity_id", sa.String()),
        sa.Column("conversation_id", sa.String()),
        sa.Column("suggestion_key", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.drop_table("suggestions")
