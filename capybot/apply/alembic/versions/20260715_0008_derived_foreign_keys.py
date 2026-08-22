"""Enforce referential integrity for Apply raw and derived records.

Revision ID: 20260715_0008
Revises: 20260715_0007
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0008"
down_revision = "20260715_0007"
branch_labels = None
depends_on = None


REQUIRED_RELATIONS = (
    ("boss_messages", "conversation_id", "boss_conversations", "CASCADE"),
    ("boss_job_cards", "conversation_id", "boss_conversations", "CASCADE"),
    ("analysis_runs", "conversation_id", "boss_conversations", "CASCADE"),
    ("opportunity_fit_results", "opportunity_id", "opportunities", "CASCADE"),
    ("agent_trace_steps", "run_id", "agent_runs", "CASCADE"),
    ("import_run_items", "import_run_id", "import_runs", "CASCADE"),
    ("contact_summaries", "contact_id", "contacts", "CASCADE"),
    ("opportunity_summaries", "opportunity_id", "opportunities", "CASCADE"),
)

NULLABLE_RELATIONS = (
    ("apply_events", "conversation_id", "boss_conversations", "CASCADE"),
    ("apply_events", "opportunity_id", "opportunities", "CASCADE"),
    ("followup_tasks", "conversation_id", "boss_conversations", "CASCADE"),
    ("followup_tasks", "opportunity_id", "opportunities", "CASCADE"),
    ("reply_drafts", "conversation_id", "boss_conversations", "CASCADE"),
    ("reply_drafts", "opportunity_id", "opportunities", "CASCADE"),
    ("review_items", "conversation_id", "boss_conversations", "CASCADE"),
    ("review_items", "opportunity_id", "opportunities", "CASCADE"),
    ("import_run_items", "conversation_id", "boss_conversations", "CASCADE"),
    ("import_run_items", "opportunity_id", "opportunities", "SET NULL"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Historical versions allowed orphaned derived rows. Clean only invalid
    # references before adding constraints; valid user data is preserved.
    for table, column, parent, _ondelete in REQUIRED_RELATIONS:
        if not _tables_exist(bind, table, parent):
            continue
        bind.execute(
            sa.text(
                f"DELETE FROM {table} child WHERE NOT EXISTS "
                f"(SELECT 1 FROM {parent} parent WHERE parent.id=child.{column})"
            )
        )
    for table, column, parent, ondelete in NULLABLE_RELATIONS:
        if not _tables_exist(bind, table, parent):
            continue
        if ondelete == "CASCADE":
            bind.execute(
                sa.text(
                    f"DELETE FROM {table} child WHERE child.{column} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {parent} parent WHERE parent.id=child.{column})"
                )
            )
        else:
            bind.execute(
                sa.text(
                    f"UPDATE {table} child SET {column}=NULL "
                    f"WHERE child.{column} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {parent} parent WHERE parent.id=child.{column})"
                )
            )

    bind.execute(
        sa.text(
            """
            DELETE FROM conversation_opportunities link
            WHERE NOT EXISTS (
                SELECT 1 FROM boss_conversations c WHERE c.id=link.conversation_id
            ) OR NOT EXISTS (
                SELECT 1 FROM opportunities o WHERE o.id=link.opportunity_id
            )
            """
        )
    )

    for table, column, parent, ondelete in (*REQUIRED_RELATIONS, *NULLABLE_RELATIONS):
        _create_fk(table, column, parent, ondelete)
    _create_fk("conversation_opportunities", "conversation_id", "boss_conversations", "CASCADE")
    _create_fk("conversation_opportunities", "opportunity_id", "opportunities", "CASCADE")


def _create_fk(table: str, column: str, parent: str, ondelete: str) -> None:
    bind = op.get_bind()
    if not _tables_exist(bind, table, parent):
        return
    inspector = sa.inspect(bind)
    if any(
        item.get("constrained_columns") == [column] and item.get("referred_table") == parent
        for item in inspector.get_foreign_keys(table)
    ):
        return
    op.create_foreign_key(
        f"fk_{table}_{column}",
        table,
        parent,
        [column],
        ["id"],
        ondelete=ondelete,
    )


def _tables_exist(bind, *names: str) -> bool:
    tables = set(sa.inspect(bind).get_table_names())
    return all(name in tables for name in names)


def downgrade() -> None:
    for table, column, _parent, _ondelete in reversed((*REQUIRED_RELATIONS, *NULLABLE_RELATIONS)):
        if _tables_exist(op.get_bind(), table):
            constraints = {
                item.get("name") for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
            }
            name = f"fk_{table}_{column}"
            if name in constraints:
                op.drop_constraint(name, table, type_="foreignkey")
    op.drop_constraint(
        "fk_conversation_opportunities_opportunity_id",
        "conversation_opportunities",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversation_opportunities_conversation_id",
        "conversation_opportunities",
        type_="foreignkey",
    )
