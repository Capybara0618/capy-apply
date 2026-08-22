import pytest
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import inspect, text

from capybot.apply.models import utc_now_iso
from capybot.apply.postgres import begin, engine
from capybot.apply.store import ApplyStore


def test_postgres_rejects_orphan_agent_trace() -> None:
    store = ApplyStore()

    with pytest.raises(ForeignKeyViolation), store.connect() as db:
        db.execute(
            """
            INSERT INTO agent_trace_steps
            (id, run_id, step_index, step_type, title, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan-trace",
                "missing-run",
                1,
                "memory_tool_result",
                "orphan",
                "{}",
                "2026-07-15T00:00:00+00:00",
            ),
        )


def test_deleting_conversation_cascades_raw_messages_and_links() -> None:
    store = ApplyStore()
    conversation_id = store.upsert_conversation(
        {
            "conversation_id": "fk-conversation",
            "boss_uid": "fk-boss",
            "contact_name": "FK test HR",
        }
    )
    store.upsert_message(
        {
            "conversation_id": conversation_id,
            "message_id": "fk-message",
            "from_me": False,
            "message_type": "text",
            "text": "Please send a resume.",
        }
    )
    opportunity_id = store.ensure_opportunities_for_conversation(conversation_id)[0]

    with store.connect() as db:
        db.execute("DELETE FROM boss_conversations WHERE id=?", (conversation_id,))
        message_count = db.execute(
            "SELECT COUNT(*) FROM boss_messages WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0]
        link_count = db.execute(
            "SELECT COUNT(*) FROM conversation_opportunities WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        opportunity_count = db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()[0]

    assert message_count == 0
    assert link_count == 0
    assert opportunity_count == 1


def test_postgres_hot_path_indexes_and_agent_run_foreign_keys_exist() -> None:
    inspector = inspect(engine())
    indexes = {
        table: {item["name"] for item in inspector.get_indexes(table)}
        for table in ("opportunities", "suggestions", "agent_trace_steps", "apply_jobs")
    }
    assert "ix_opportunities_account_stage_updated" in indexes["opportunities"]
    assert "ix_suggestions_kind_status_due" in indexes["suggestions"]
    assert "uq_agent_trace_run_step" in indexes["agent_trace_steps"]
    assert "ix_apply_jobs_account_created" in indexes["apply_jobs"]

    foreign_keys = {
        tuple(item["constrained_columns"]): item["referred_table"]
        for item in inspector.get_foreign_keys("agent_runs")
    }
    assert foreign_keys[("conversation_id",)] == "boss_conversations"
    assert foreign_keys[("opportunity_id",)] == "opportunities"


def test_pooled_compat_cursor_does_not_leak_dict_row_into_sqlalchemy() -> None:
    store = ApplyStore()
    with store.connect() as db:
        assert db.execute("SELECT 1 AS value").fetchone()["value"] == 1

    with begin() as db:
        assert db.execute(text("SELECT 1 AS value")).scalar() == 1

    with store.connect() as db:
        assert db.execute("SELECT 2 AS value").fetchone()["value"] == 2


def test_contact_summary_uses_existing_contact_primary_key_after_upsert_conflict() -> None:
    store = ApplyStore()
    conversation_id = store.upsert_conversation(
        {
            "conversation_id": "existing-contact-conversation",
            "boss_uid": "existing-platform-uid",
            "contact_name": "王女士",
            "company": "示例科技",
        }
    )
    account_id = store.current_account_id()
    now = utc_now_iso()
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO contacts
            (id, account_id, platform, platform_uid, name, company, created_at, updated_at)
            VALUES (?, ?, 'boss', ?, ?, ?, ?, ?)
            """,
            (
                "existing-contact-id",
                account_id,
                "existing-platform-uid",
                "旧联系人",
                "示例科技",
                now,
                now,
            ),
        )

    opportunity_id = store.ensure_opportunities_for_conversation(conversation_id)[0]
    store.save_opportunity_analysis(
        opportunity_id,
        {
            "pipeline_stage": "communicating",
            "events": [],
            "tasks": [],
            "reply_draft": {"content": "", "evidence_message_ids": []},
            "risk_flags": [],
            "summary_update": {
                "opportunity_summary": "正在沟通。",
                "contact_summary": "联系人正在确认候选人信息。",
            },
        },
        conversation_id=conversation_id,
    )

    with store.connect() as db:
        summary = db.execute("SELECT contact_id FROM contact_summaries").fetchone()
    assert summary["contact_id"] == "existing-contact-id"
