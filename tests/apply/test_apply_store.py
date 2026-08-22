from pathlib import Path

import pytest

from capybot.apply.decision_router import DecisionRouter
from capybot.apply.store import ApplyStore


@pytest.fixture(autouse=True)
def disable_real_apply_llm(monkeypatch):
    monkeypatch.setenv("CAPYBOT_APPLY_DISABLE_LLM", "1")


def test_store_dedupes_messages_and_builds_action_views(tmp_path: Path):
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "boss_1",
            "boss_uid": "1",
            "contact_name": "刘女士",
            "company": "示例 AI",
        }
    )
    store.upsert_job_card(cid, {"title": "AI Agent 开发实习生", "company": "示例 AI"})
    _, inserted1 = store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m1",
            "from_me": False,
            "sender_name": "刘女士",
            "text": "你今天把简历和 GitHub 发我。",
            "message_type": "text",
        }
    )
    _, inserted2 = store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m1",
            "from_me": False,
            "sender_name": "HR Liu",
            "text": "duplicate",
            "message_type": "text",
        }
    )
    assert inserted1 is True
    assert inserted2 is False

    oid = store.ensure_opportunities_for_conversation(cid)[0]
    store.save_opportunity_analysis(
        oid,
        {
            "pipeline_stage": "need_my_action",
            "stage_reason": "HR 请求发送简历和 GitHub。",
            "confidence": 0.95,
            "events": [
                {
                    "event_type": "material_requested",
                    "title": "HR 请求候选材料",
                    "detail": "需要发送简历和 GitHub。",
                    "evidence_message_ids": ["m1"],
                }
            ],
            "tasks": [
                {
                    "title": "发送简历和 GitHub",
                    "due_at": None,
                    "priority": "high",
                    "reason": "HR 明确请求候选材料。",
                    "evidence_message_ids": ["m1"],
                }
            ],
            "reply_draft": {"content": "", "reason": "", "evidence_message_ids": []},
            "risk_flags": [],
            "next_action": "发送简历和 GitHub",
            "summary_update": {
                "opportunity_summary": "HR 正在等待候选材料。",
                "contact_summary": "联系人已请求简历和项目链接。",
            },
        },
        conversation_id=cid,
    )
    assert store.tasks_payload()["suggestions"]
    assert store.opportunities()[0]["title"] == "AI Agent 开发实习生"


def test_store_supersedes_only_pending_generated_drafts_with_placeholders():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "boss_unsafe_draft",
            "boss_uid": "unsafe-draft",
            "contact_name": "陈女士",
            "company": "示例科技",
        }
    )
    oid = store.ensure_opportunities_for_conversation(cid)[0]
    now = "2026-07-28T00:00:00+00:00"
    with store.connect() as db:
        unsafe = store._upsert_suggestion(
            db,
            opportunity_id=oid,
            conversation_id=cid,
            kind="draft",
            content="您好，我是[您的名字]，最近参与了[项目名称]。",
            evidence_refs=[],
            now=now,
        )
        safe = store._upsert_suggestion(
            db,
            opportunity_id=oid,
            conversation_id=cid,
            kind="draft",
            content="您好，感谢回复，我对这个机会很感兴趣。",
            evidence_refs=[],
            now=now,
        )

    result = store.supersede_unsafe_drafts(force=True)
    with store.connect() as db:
        rows = {
            row["fingerprint"]: row["status"]
            for row in db.execute(
                "SELECT fingerprint, status FROM suggestions WHERE opportunity_id=?",
                (oid,),
            ).fetchall()
        }

    assert result["changed"] == 1
    assert rows[unsafe] == "superseded"
    assert rows[safe] == "suggested"


def test_action_ranking_excludes_waiting_feedback_without_user_task():
    actions = ApplyStore._rank_action_items(
        [
            {
                "id": "waiting",
                "stage": "waiting_feedback",
                "next_action": "等待 HR 反馈",
            },
            {
                "id": "mine",
                "stage": "need_my_action",
                "next_action": "回复 HR 的项目问题",
            },
        ],
        [],
        [],
    )

    assert [item["opportunity"]["id"] for item in actions] == ["mine"]


def test_job_detail_refresh_updates_existing_card_without_counting_as_new():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "boss_job_refresh",
            "boss_uid": "job-refresh",
            "contact_name": "招聘负责人",
            "company": "示例科技",
        }
    )
    first_id, first_inserted = store.upsert_job_card(
        cid,
        {
            "platform_job_id": "encrypted-job-1",
            "title": "Agent 实习生",
            "company": "示例科技",
            "raw_payload": {"jobId": "encrypted-job-1"},
        },
    )
    refreshed_id, refreshed_inserted = store.upsert_job_card(
        cid,
        {
            "platform_job_id": "encrypted-job-1",
            "title": "Agent 开发实习生",
            "company": "示例科技有限公司",
            "salary": "200-250元/天",
            "city": "杭州",
            "raw_payload": {
                "jobInfo": {
                    "encryptJobId": "encrypted-job-1",
                    "postDescription": "负责 Agent 工具调用与评估。",
                }
            },
        },
    )

    context = store.opportunity_context(
        store.ensure_opportunities_for_conversation(cid)[0]
    )

    assert first_inserted is True
    assert refreshed_inserted is False
    assert refreshed_id == first_id
    assert len(context["jobs"]) == 1
    assert context["jobs"][0]["title"] == "Agent 开发实习生"
    assert context["jobs"][0]["salary"] == "200-250元/天"


def test_vip_auto_followup_is_ignored_but_candidate_message_is_analyzable(tmp_path: Path):
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "boss_vip",
            "boss_uid": "2",
            "contact_name": "陆飞",
            "company": "示例科技",
        }
    )
    store.upsert_job_card(cid, {"title": "后端开发实习生", "company": "示例科技"})
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m1",
            "from_me": True,
            "sender_name": "我",
            "text": "您好，看到贵司在招聘后端开发实习生，我目前主要做 RAG 与 LangGraph Agent Workflow 相关项目，想争取一个面试机会。",
            "message_type": "text",
        }
    )
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m2",
            "from_me": False,
            "sender_name": "系统",
            "text": "system",
            "message_type": "system",
            "is_human_message": 0,
        }
    )
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m3",
            "from_me": False,
            "sender_name": "陆飞",
            "text": "VIP求职助手帮你追聊",
            "message_type": "auto_followup",
            "is_human_message": 0,
        }
    )
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "m4",
            "from_me": True,
            "sender_name": "我",
            "text": "我对这个岗位很感兴趣，期待您的回复~",
            "message_type": "text",
        }
    )

    messages = store.message_evidence(["m2", "m3"])["messages"]
    ignored = DecisionRouter.route_delta(
        messages,
        source_quality="cold_outreach_vip_no_reply",
    )
    changed = DecisionRouter.route_delta(
        store.message_evidence(["m4"])["messages"],
        source_quality="cold_outreach_vip_no_reply",
    )

    assert ignored.mode == "skip"
    assert changed.mode == "agent"
    assert changed.trigger_type == "candidate_message"


def test_store_rejects_actions_that_conflict_with_discovered_stage():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "action-gate",
            "boss_uid": "action-gate-boss",
            "contact_name": "测试 HR",
        }
    )
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "action-gate-message",
            "from_me": True,
            "message_type": "text",
            "text": "您好，我对这个岗位很感兴趣。",
        }
    )
    oid = store.ensure_opportunities_for_conversation(cid)[0]

    store.save_opportunity_analysis(
        oid,
        {
            "pipeline_stage": "discovered",
            "confidence": 0.9,
            "source_quality": "cold_outreach_no_reply",
            "events": [],
            "tasks": [
                {
                    "title": "准备面试材料",
                    "evidence_message_ids": ["action-gate-message"],
                }
            ],
            "reply_draft": {
                "content": "您好，我可以参加面试。",
                "evidence_message_ids": ["action-gate-message"],
            },
            "risk_flags": [],
            "summary_update": {"opportunity_summary": "等待真实 HR 回复。"},
        },
        conversation_id=cid,
    )

    detail = store.opportunity_detail(oid)
    assert detail["tasks"] == []
    assert detail["drafts"] == []


def test_store_only_marks_real_stage_changes_as_progress_and_writes_l2_event():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "stage-progress",
            "boss_uid": "stage-progress-boss",
            "contact_name": "测试 HR",
        }
    )
    store.upsert_message(
        {
            "conversation_id": cid,
            "message_id": "stage-progress-message",
            "from_me": True,
            "message_type": "text",
            "text": "您好，我已经发送简历。",
        }
    )
    oid = store.ensure_opportunities_for_conversation(cid)[0]
    original_progress = "2026-07-01T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            "UPDATE opportunities SET last_progress_at=? WHERE id=?",
            (original_progress, oid),
        )

    base_result = {
        "confidence": 0.9,
        "events": [],
        "tasks": [],
        "reply_draft": {"content": "", "evidence_message_ids": []},
        "risk_flags": [],
        "evidence_message_ids": ["stage-progress-message"],
        "summary_update": {"opportunity_summary": "候选人已发送简历。"},
    }
    store.save_opportunity_analysis(
        oid,
        {**base_result, "pipeline_stage": "discovered"},
        conversation_id=cid,
    )
    unchanged = store.opportunity_detail(oid)
    assert unchanged["opportunity"]["last_progress_at"] == original_progress
    assert unchanged["events"] == []

    store.save_opportunity_analysis(
        oid,
        {**base_result, "pipeline_stage": "waiting_feedback"},
        conversation_id=cid,
    )
    changed = store.opportunity_detail(oid)
    assert changed["opportunity"]["last_progress_at"] != original_progress
    assert changed["events"][0]["event_type"] == "stage_changed"
    assert "discovered -> waiting_feedback" in changed["events"][0]["detail"]


def test_store_closes_stage_review_already_reflected_by_opportunity():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "satisfied-stage-review",
            "boss_uid": "satisfied-stage-boss",
            "contact_name": "测试 HR",
        }
    )
    oid = store.ensure_opportunities_for_conversation(cid)[0]
    now = "2026-07-15T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            """
            INSERT INTO suggestions
            (id, kind, content, payload, status, evidence_refs, opportunity_id,
             conversation_id, fingerprint, created_at, updated_at)
            VALUES (?, 'stage', '阶段建议', ?, 'suggested', '[]', ?, ?, ?, ?, ?)
            """,
            (
                "satisfied-stage-review-id",
                '{"stage":"discovered"}',
                oid,
                cid,
                "satisfied-stage-key",
                now,
                now,
            ),
        )

    store.save_opportunity_analysis(
        oid,
        {
            "pipeline_stage": "discovered",
            "confidence": 0.9,
            "events": [],
            "tasks": [],
            "reply_draft": {"content": "", "evidence_message_ids": []},
            "risk_flags": [],
            "summary_update": {"opportunity_summary": "等待真实 HR 回复。"},
        },
        conversation_id=cid,
    )

    assert store.tasks_payload()["suggestions"] == []


def test_pending_stage_suggestion_is_projected_without_overwriting_current_stage():
    store = ApplyStore()
    cid = store.upsert_conversation(
        {
            "conversation_id": "pending-stage-projection",
            "boss_uid": "pending-stage-boss",
            "contact_name": "测试 HR",
        }
    )
    oid = store.ensure_opportunities_for_conversation(cid)[0]
    now = "2026-07-15T00:00:00+00:00"
    with store.connect() as db:
        db.execute(
            "UPDATE opportunities SET stage='waiting_feedback' WHERE id=?",
            (oid,),
        )
        db.execute(
            """
            INSERT INTO suggestions
            (id, kind, content, payload, status, evidence_refs, opportunity_id,
             conversation_id, fingerprint, created_at, updated_at)
            VALUES (?, 'stage', '阶段建议：待我行动', ?, 'suggested', '[]', ?, ?, ?, ?, ?)
            """,
            (
                "pending-stage-projection-id",
                '{"stage":"need_my_action"}',
                oid,
                cid,
                "pending-stage-projection-key",
                now,
                now,
            ),
        )

    listed = store.opportunities()[0]
    detailed = store.opportunity_detail(oid)["opportunity"]
    overview = store.overview()

    assert listed["stage"] == "waiting_feedback"
    assert listed["stage_suggestion"] == "need_my_action"
    assert detailed["stage"] == "waiting_feedback"
    assert detailed["stage_suggestion"] == "need_my_action"
    assert overview["metrics"]["need_my_action"] == 1
    assert overview["action_items"][0]["priority"] == "high"


def test_boss_job_assistant_conversation_does_not_create_opportunity():
    store = ApplyStore()
    conversation_id = store.upsert_conversation(
        {
            "conversation_id": "boss-assistant",
            "boss_uid": "boss-assistant",
            "contact_name": "10:51求职助手您正在与Boss求职助手沟通",
            "last_message_preview": "您正在与Boss求职助手沟通",
        }
    )

    assert store.ensure_opportunities_for_conversation(conversation_id) == []
    assert store.opportunities() == []
