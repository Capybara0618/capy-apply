"""add account isolation and query indexes

Revision ID: 20260713_0002
Revises: 20260524_0001
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713_0002"
down_revision = "20260524_0001"
branch_labels = None
depends_on = None


ACCOUNT_TABLES = (
    "boss_conversations",
    "boss_messages",
    "boss_job_cards",
    "contacts",
    "opportunities",
    "agent_runs",
    "import_runs",
    "apply_jobs",
)
REQUIRED_ACCOUNT_TABLES = {
    "boss_conversations",
    "boss_messages",
    "boss_job_cards",
    "contacts",
    "opportunities",
}


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _constraints(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    names = {item["name"] for item in inspector.get_unique_constraints(table) if item.get("name")}
    names.update(item["name"] for item in inspector.get_foreign_keys(table) if item.get("name"))
    return names


def _indexes(table: str) -> set[str]:
    return {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table) if item.get("name")
    }


def _has_account_fk(table: str) -> bool:
    return any(
        item.get("constrained_columns") == ["account_id"]
        and item.get("referred_table") == "apply_accounts"
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    account_id = bind.execute(
        sa.text(
            "SELECT id FROM apply_accounts ORDER BY COALESCE(last_import_at, last_seen_at) DESC LIMIT 1"
        )
    ).scalar()
    if not account_id:
        account_id = "boss_account_local"
        bind.execute(
            sa.text("""
            INSERT INTO apply_accounts
            (id, platform, display_name, source, raw_payload, first_seen_at, last_seen_at)
            VALUES (:id, 'boss', 'BOSS 本地账号', 'migration', '{}', now()::text, now()::text)
        """),
            {"id": account_id},
        )

    for table in ACCOUNT_TABLES:
        if "account_id" not in _columns(table):
            op.add_column(table, sa.Column("account_id", sa.String(), nullable=True))
        bind.execute(
            sa.text(f"UPDATE {table} SET account_id=:account_id WHERE account_id IS NULL"),
            {"account_id": account_id},
        )
        fk_name = f"fk_{table}_account_id"
        if not _has_account_fk(table):
            op.create_foreign_key(
                fk_name, table, "apply_accounts", ["account_id"], ["id"], ondelete="CASCADE"
            )
        if table in REQUIRED_ACCOUNT_TABLES:
            op.alter_column(table, "account_id", nullable=False)

    message_constraints = _constraints("boss_messages")
    if "boss_messages_message_id_key" in message_constraints:
        op.drop_constraint("boss_messages_message_id_key", "boss_messages", type_="unique")
    if "uq_boss_messages_account_message" not in _constraints("boss_messages"):
        op.create_unique_constraint(
            "uq_boss_messages_account_message",
            "boss_messages",
            ["account_id", "message_id"],
        )

    contact_constraints = _constraints("contacts")
    if "uq_contacts_platform_uid" in contact_constraints:
        op.drop_constraint("uq_contacts_platform_uid", "contacts", type_="unique")
    if "uq_contacts_account_platform_uid" not in _constraints("contacts"):
        op.create_unique_constraint(
            "uq_contacts_account_platform_uid",
            "contacts",
            ["account_id", "platform", "platform_uid"],
        )

    indexes = {
        "boss_conversations": (
            "ix_boss_conversations_account_updated",
            ["account_id", "updated_at"],
        ),
        "boss_messages": ("ix_boss_messages_conversation_sent", ["conversation_id", "sent_at"]),
        "opportunities": ("ix_opportunities_account_updated", ["account_id", "updated_at"]),
    }
    for table, (name, columns) in indexes.items():
        if name not in _indexes(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    raise RuntimeError("账号隔离迁移包含数据归属变更，不支持自动降级。")
