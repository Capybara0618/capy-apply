"""Scope candidate profile and preferences by BOSS account.

Revision ID: 20260715_0007
Revises: 20260714_0006
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0007"
down_revision = "20260714_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    account_id = bind.execute(
        sa.text(
            "SELECT id FROM apply_accounts "
            "ORDER BY COALESCE(last_import_at, last_seen_at) DESC LIMIT 1"
        )
    ).scalar()
    if not account_id:
        account_id = "boss_account_local"
        bind.execute(
            sa.text(
                """
                INSERT INTO apply_accounts
                (id, platform, display_name, source, raw_payload, first_seen_at, last_seen_at)
                VALUES (:id, 'boss', 'BOSS local account', 'migration', '{}',
                        now()::text, now()::text)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": account_id},
        )

    for table in ("candidate_profile", "job_preferences"):
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "account_id" not in columns:
            op.add_column(table, sa.Column("account_id", sa.String(), nullable=True))
        bind.execute(
            sa.text(f"UPDATE {table} SET account_id=:account_id WHERE account_id IS NULL"),
            {"account_id": account_id},
        )
        op.alter_column(table, "account_id", nullable=False)

        inspector = sa.inspect(bind)
        primary_key = inspector.get_pk_constraint(table)
        if primary_key.get("name"):
            op.drop_constraint(primary_key["name"], table, type_="primary")
        op.create_primary_key(f"pk_{table}_account_id_id", table, ["account_id", "id"])

        inspector = sa.inspect(bind)
        foreign_keys = inspector.get_foreign_keys(table)
        if not any(item.get("constrained_columns") == ["account_id"] for item in foreign_keys):
            op.create_foreign_key(
                f"fk_{table}_account_id",
                table,
                "apply_accounts",
                ["account_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    raise RuntimeError(
        "Candidate profiles and preferences are account-scoped; automatic downgrade is unsafe."
    )
