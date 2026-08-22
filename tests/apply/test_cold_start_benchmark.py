from __future__ import annotations

import pytest

from capybot.apply.agent_runtime.commit_gate import CommitGate
from capybot.apply.decision_router import DecisionRouter
from capybot.evaluation.cold_start_benchmark import (
    _deterministic_reference,
    _load_external_references,
    _percentile,
    benchmark_database_url,
    reset_benchmark_database,
)


def test_benchmark_url_preserves_password_without_targeting_production() -> None:
    url = benchmark_database_url(
        "postgresql+psycopg://capybot:secret@127.0.0.1:15432/capybot_apply"
    )

    assert "secret" in url
    assert url.endswith("/capybot_apply_benchmark")


def test_reset_rejects_non_benchmark_database_before_connecting() -> None:
    with pytest.raises(ValueError, match="拒绝重置"):
        reset_benchmark_database(
            "postgresql+psycopg://capybot:secret@127.0.0.1:15432/capybot_apply"
        )


@pytest.mark.parametrize(
    ("latest", "expected"),
    [
        (
            {"from_me": False, "text": "方便发一份简历吗？"},
            {"stage": "need_my_action", "action": "send_material"},
        ),
        (
            {"from_me": False, "text": "周三下午方便面试吗？"},
            {"stage": "interviewing", "action": "confirm_interview"},
        ),
        (
            {"from_me": False, "text": "这个岗位已经招满了"},
            {"stage": "closed", "action": "close"},
        ),
        (
            {"from_me": True, "text": "您好，这是我的项目介绍"},
            {"stage": "discovered", "action": "wait"},
        ),
    ],
)
def test_deterministic_reference_only_labels_unambiguous_latest_message(
    latest: dict[str, object],
    expected: dict[str, str],
) -> None:
    assert _deterministic_reference([latest]) == expected


def test_reference_keeps_rejection_closed_after_candidate_acknowledgement() -> None:
    messages = [
        {"from_me": False, "text": "您的经历与岗位不匹配，祝好"},
        {"from_me": True, "text": "好的，谢谢"},
    ]

    assert _deterministic_reference(messages) == {
        "stage": "closed",
        "action": "close",
    }


def test_reference_ignores_platform_greeting_editor_prompt() -> None:
    messages = [
        {"from_me": False, "text": "点击修改打招呼语  去修改"},
        {"from_me": True, "text": "您好，我想了解这个岗位"},
    ]

    assert _deterministic_reference(messages) == {
        "stage": "discovered",
        "action": "wait",
    }


def test_external_reference_loader_excludes_non_cases(tmp_path) -> None:
    path = tmp_path / "reference.json"
    path.write_text(
        """
        {
          "dataset_kind": "llm_double_annotated_adjudicated_reference",
          "annotations": [
            {"case_id": "case_a", "include": true, "stage": "closed", "action": "close"},
            {"case_id": "case_b", "include": false, "stage": null, "action": null}
          ]
        }
        """,
        encoding="utf-8",
    )
    assert _load_external_references(path) == {
        "case_a": {"stage": "closed", "action": "close"}
    }


def test_percentile_is_stable_for_small_samples() -> None:
    assert _percentile([10, 40, 20, 30], 0.5) == 30
    assert _percentile([10, 40, 20, 30], 0.95) == 40


def test_decision_router_skips_llm_only_without_real_hr_reply() -> None:
    candidate_only = {
        "messages": [
            {
                "message_id": "m1",
                "from_me": True,
                "message_type": "text",
                "is_human_message": 1,
            },
            {
                "message_id": "m2",
                "from_me": True,
                "message_type": "file",
                "is_human_message": 1,
            },
        ]
    }
    with_hr = {
        "messages": [
            *candidate_only["messages"],
            {
                "message_id": "m3",
                "from_me": False,
                "message_type": "text",
                "is_human_message": 1,
            },
        ]
    }

    route = DecisionRouter.route_context(
        candidate_only,
        {"type": "cold_start"},
    )

    decision = route.decision
    assert route.mode == "cold_projection"
    assert decision is not None
    assert decision["stage"] == "discovered"
    assert decision["next"]["owner"] == "none"
    assert decision["suggestions"] == []
    assert (
        DecisionRouter.route_context(
            with_hr,
            {"type": "cold_start"},
        ).mode
        == "agent"
    )


def test_decision_router_repairs_platform_rejected_attachment() -> None:
    context = {
        "messages": [
            {
                "message_id": "m1",
                "from_me": True,
                "message_type": "text",
                "is_human_message": 1,
                "text": "简历附后",
            },
            {
                "message_id": "m2",
                "from_me": True,
                "message_type": "image",
                "is_human_message": 1,
                "text": "",
            },
            {
                "message_id": "m3",
                "from_me": False,
                "message_type": "system",
                "is_human_message": 0,
                "text": "图片中可能存在联系方式，请打码后再发",
            },
        ]
    }

    route = DecisionRouter.route_context(
        context,
        {"type": "cold_start"},
    )

    decision = route.decision
    assert route.mode == "cold_projection"
    assert decision is not None
    assert decision["stage"] == "discovered"
    assert decision["next"]["action"] == "send_material"
    assert {item["kind"] for item in decision["suggestions"]} == {"task", "draft"}
    assert CommitGate().validate(
        decision,
        valid_evidence_refs={"boss_message:m2", "boss_message:m3"},
        preserve_discovered_stage=True,
    ).accepted


def test_commit_gate_normalizes_closed_stage_to_unique_close_action() -> None:
    payload = {
        "status": "ready",
        "stage": "closed",
        "summary": "HR 已明确结束流程。",
        "evidence": ["boss_message:r1"],
        "next": {
            "action": "wait",
            "owner": "hr",
            "when": "after_48h",
            "reason": "模型错误地建议继续等待。",
            "evidence": ["boss_message:r1"],
        },
        "changes": [],
        "suggestions": [],
        "confidence": 0.9,
    }

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:r1"},
    )

    assert result.accepted
    assert result.decision["next"]["action"] == "close"
    assert result.decision["next"]["owner"] == "none"


def test_commit_gate_reuses_unique_top_level_evidence_for_next() -> None:
    payload = {
        "status": "ready",
        "stage": "waiting_feedback",
        "summary": "候选人已完成回复，等待 HR。",
        "evidence": ["boss_message:m1"],
        "next": {
            "action": "wait",
            "owner": "hr",
            "when": "after_48h",
            "reason": "等待反馈。",
            "evidence": [],
        },
        "changes": [],
        "suggestions": [],
        "confidence": 0.9,
    }

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted
    assert result.decision["next"]["evidence"] == ["boss_message:m1"]


def test_commit_gate_allows_model_to_choose_action_for_pending_hr_question() -> None:
    payload = {
        "status": "ready",
        "stage": "need_my_action",
        "summary": "HR 请求简历。",
        "evidence": ["boss_message:m1"],
        "next": {
            "action": "reply",
            "owner": "me",
            "when": "now",
            "reason": "回复 HR。",
            "evidence": ["boss_message:m1"],
        },
        "changes": [],
        "suggestions": [
            {
                "kind": "task",
                "content": "回复 HR。",
                "evidence": ["boss_message:m1"],
            },
            {
                "kind": "draft",
                "content": "您好，感谢联系。",
                "evidence": ["boss_message:m1"],
            },
        ],
        "confidence": 0.9,
    }

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        pending_hr_question_refs={"boss_message:m1"},
    )

    assert result.accepted
    assert result.decision["next"]["action"] == "reply"
