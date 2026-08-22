import json

from capybot.apply.models import utc_now_iso
from capybot.apply.store import ApplyStore
from capybot.apply.tasks import _recompute_import_analysis_report


def test_import_analysis_report_is_recomputed_idempotently():
    store = ApplyStore()
    now = utc_now_iso()
    conversation_id = store.upsert_conversation(
        {
            "conversation_id": "import-idempotent-conversation",
            "boss_uid": "import-idempotent-boss",
            "contact_name": "Import test HR",
        }
    )
    opportunity_id = store.ensure_opportunities_for_conversation(conversation_id)[0]
    with store.connect() as db:
        db.execute(
            "INSERT INTO import_runs (id, account_id, started_at, report) VALUES (?, ?, ?, ?)",
            (
                "import-idempotent",
                "test_account",
                now,
                json.dumps({"analyzed_opportunities": 99, "queued_opportunities": 99}),
            ),
        )
        db.execute(
            """
            INSERT INTO import_run_items
            (id, import_run_id, opportunity_id, new_message_ids, new_message_count,
             analysis_mode, after_stage, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "item-done",
                "import-idempotent",
                opportunity_id,
                "[]",
                1,
                "opportunity_agent",
                "waiting_feedback",
                now,
            ),
        )
        db.execute(
            """
            INSERT INTO import_run_items
            (id, import_run_id, opportunity_id, new_message_ids, new_message_count,
             analysis_mode, after_stage, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "item-queued",
                "import-idempotent",
                opportunity_id,
                "[]",
                1,
                "opportunity_agent",
                None,
                now,
            ),
        )

    _recompute_import_analysis_report("import-idempotent")
    _recompute_import_analysis_report("import-idempotent")

    with store.connect() as db:
        row = db.execute(
            "SELECT report FROM import_runs WHERE id=?", ("import-idempotent",)
        ).fetchone()
    report = json.loads(row["report"])
    assert report["analyzed_opportunities"] == 1
    assert report["queued_opportunities"] == 1
