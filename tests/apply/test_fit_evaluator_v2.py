from __future__ import annotations

import json
from typing import Any

import pytest

from capybot.apply.agent_runtime.fit_evaluator import (
    HARD_CAP_LIMITS,
    FitCap,
    FitDimension,
    JobFitDecision,
    JobFitEvaluator,
)
from capybot.apply.agent_runtime.model import ModelTurn
from capybot.apply.agent_runtime.priority import OpportunityPriorityCalculator
from capybot.apply.agent_runtime.skills import ApplySkillLibrary


class FakeStore:
    def __init__(self, *, with_profile: bool = True) -> None:
        self.with_profile = with_profile

    def opportunity_context(self, _opportunity_id: str) -> dict[str, Any]:
        return {
            "opportunity": {
                "id": "opp-1",
                "stage": "need_my_action",
                "risk_flags": "[]",
            },
            "messages": [
                {
                    "message_id": "m1",
                    "from_me": False,
                    "is_human_message": 1,
                    "sent_at": "2026-07-25T10:00:00+00:00",
                }
            ],
            "candidate_profile": {
                "resume_markdown": "# 简历\nPython、MCP、Agent",
                "profile_summary": "Agent 开发候选人",
                "skill_tags": ["Python", "MCP"],
                "project_tags": ["Tool-Calling Agent"],
            }
            if self.with_profile
            else None,
            "job_preferences": {"target_roles": "Agent 开发实习"},
            "job_snapshots": [
                {
                    "id": "job-1",
                    "payload": json.dumps(
                        {
                            "title": "Agent 开发实习生",
                            "company": "示例科技",
                            "city": "杭州",
                            "requirements": "Python、MCP",
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "jobs": [],
        }


class FakeFitModel:
    provider_label = "fake"
    model_label = "fake-fit"

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any],
        schema_name: str,
    ) -> ModelTurn:
        profile_ref = (
            "candidate_profile:"
            + JobFitEvaluator._hash(
                {
                    "resume": "# 简历\nPython、MCP、Agent",
                    "preferences": {"target_roles": "Agent 开发实习"},
                }
            )[:16]
        )
        return ModelTurn(
            content=json.dumps(
                {
                    "status": "ok",
                    "summary": "技能和项目方向匹配。",
                    "dimensions": [
                        {
                            "name": name,
                            "score": score,
                            "reason": f"{name}有可引用依据。",
                            "evidence": [profile_ref, "boss_job_snapshot:job-1"],
                        }
                        for name, score in [
                            ("目标方向", 90),
                            ("核心技能", 90),
                            ("项目经历", 80),
                            ("Agent/LLM 相关性", 95),
                            ("地点与实习时间", 80),
                            ("薪资与风险", 70),
                        ]
                    ],
                    "gaps": [],
                    "hard_caps": [],
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            ),
            prompt_tokens=100,
            completion_tokens=80,
            response_mode="json_schema",
        )


@pytest.mark.asyncio
async def test_fit_evaluator_scores_only_with_profile_and_job_evidence() -> None:
    result = await JobFitEvaluator(FakeStore(), model=FakeFitModel()).evaluate("opp-1")

    assert result["status"] == "ok"
    assert result["job_fit_score"] == 85
    assert result["opportunity_priority_score"] > 0
    assert result["dimensions"][0]["evidence"]
    assert result["_metrics"]["policy_hash"]


@pytest.mark.asyncio
async def test_supported_low_fit_is_valid_even_when_model_requests_review() -> None:
    class ConservativeModel(FakeFitModel):
        async def complete_json(self, *args: Any, **kwargs: Any) -> ModelTurn:
            turn = await super().complete_json(*args, **kwargs)
            payload = json.loads(turn.content or "{}")
            payload["status"] = "needs_review"
            payload["confidence"] = 0.75
            return ModelTurn(
                content=json.dumps(payload, ensure_ascii=False),
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
                response_mode=turn.response_mode,
            )

    result = await JobFitEvaluator(
        FakeStore(),
        model=ConservativeModel(),
    ).evaluate("opp-1")

    assert result["status"] == "ok"
    assert "满足可复现评分阈值" in result["raw_model_output"]["assessment_status_reason"]


@pytest.mark.asyncio
async def test_fit_without_job_requirements_stays_needs_review() -> None:
    class MissingRequirementsStore(FakeStore):
        def opportunity_context(self, opportunity_id: str) -> dict[str, Any]:
            context = super().opportunity_context(opportunity_id)
            context["job_snapshots"][0]["payload"] = json.dumps(
                {
                    "title": "Agent 开发实习生",
                    "company": "示例科技",
                    "salary": "200-300元/天",
                    "city": "杭州",
                    "experience": "在校生",
                },
                ensure_ascii=False,
            )
            return context

    result = await JobFitEvaluator(
        MissingRequirementsStore(),
        model=FakeFitModel(),
    ).evaluate("opp-1")

    assert result["status"] == "needs_review"
    assert "缺少职责或任职要求" in result["raw_model_output"]["assessment_status_reason"]


@pytest.mark.asyncio
async def test_fit_evaluator_does_not_call_llm_without_profile() -> None:
    result = await JobFitEvaluator(
        FakeStore(with_profile=False),
        model=FakeFitModel(),
    ).evaluate("opp-1")

    assert result["status"] == "no_profile"
    assert result["job_fit_score"] is None


def test_priority_breakdown_is_transparent_and_bounded() -> None:
    context = FakeStore().opportunity_context("opp-1")
    result = OpportunityPriorityCalculator.calculate(context, fit_score=80)

    assert 0 <= result["score"] <= 100
    assert set(result["breakdown"]) == {
        "fit",
        "hr_interaction",
        "urgency",
        "recency",
        "risk_penalty",
    }


def test_imported_job_card_is_valid_version_zero_fit_evidence() -> None:
    facts, refs = JobFitEvaluator._job_facts(
        {
            "job_snapshots": [],
            "jobs": [
                {
                    "id": "jobcard-1",
                    "title": "Agent 开发实习生",
                    "company": "示例科技",
                    "city": "杭州",
                    "raw_payload": json.dumps(
                        {"positionCategory": "Python", "content": "熟悉 MCP"},
                        ensure_ascii=False,
                    ),
                }
            ],
        }
    )

    assert refs == ["boss_job_snapshot:jobcard-1"]
    assert facts[0]["requirements"] == "熟悉 MCP"


def test_hr_chat_can_supply_missing_job_requirements_with_message_evidence() -> None:
    facts, refs = JobFitEvaluator._job_facts(
        {
            "jobs": [
                {
                    "id": "jobcard-1",
                    "title": "Agent 开发实习生",
                    "company": "示例科技",
                    "raw_payload": "{}",
                }
            ],
            "messages": [
                {
                    "message_id": "m1",
                    "from_me": False,
                    "is_human_message": 1,
                    "text": "岗位主要负责 MCP 工具接入和 Agent 评估。",
                },
                {
                    "message_id": "m2",
                    "from_me": True,
                    "is_human_message": 1,
                    "text": "我的项目也使用了 MCP。",
                },
            ],
        }
    )

    assert refs == ["boss_job_snapshot:jobcard-1", "boss_message:m1"]
    assert facts[1] == {
        "ref": "boss_message:m1",
        "requirements": "岗位主要负责 MCP 工具接入和 Agent 评估。",
        "source": "HR 聊天补充",
    }


def test_fit_dimension_requires_both_candidate_and_job_evidence() -> None:
    with pytest.raises(ValueError, match="候选人证据和岗位证据"):
        FitDimension(
            name="核心技能",
            score=90,
            reason="不能只根据简历声称匹配。",
            evidence=["candidate_profile:abc"],
        )


def test_fit_dimension_accepts_hr_message_as_job_evidence() -> None:
    dimension = FitDimension(
        name="核心技能",
        score=90,
        reason="HR 明确补充了岗位技能要求。",
        evidence=["candidate_profile:abc", "boss_message:m1"],
    )

    assert dimension.evidence[-1] == "boss_message:m1"


def test_fit_evidence_normalization_only_recovers_known_unique_refs() -> None:
    valid = {
        "candidate_profile:abc",
        "boss_job_snapshot:job-1",
    }
    payload = {
        "dimensions": [
            {
                "evidence": [
                    "profile:candidate_profile:abc:skills",
                    "jobs:boss_job_snapshot:job-1:salary",
                    "boss_job_snapshot:unknown",
                ]
            }
        ]
    }

    normalized = JobFitEvaluator._normalize_evidence_refs(payload, valid)

    assert normalized["dimensions"][0]["evidence"] == [
        "candidate_profile:abc",
        "boss_job_snapshot:job-1",
        "boss_job_snapshot:unknown",
    ]


def test_hard_cap_score_is_owned_by_code_policy() -> None:
    cap = FitCap(
        type="city_mismatch",
        reason="岗位城市与求职偏好冲突。",
        evidence=["candidate_profile:abc", "boss_job_snapshot:job-1"],
    )

    assert "max_score" not in cap.model_dump()
    assert HARD_CAP_LIMITS[cap.type] == 60


def test_hard_cap_policy_rejects_covered_schedule_and_overlapping_salary() -> None:
    schedule = FitCap(
        type="internship_duration_mismatch",
        reason="模型提议周期冲突。",
        evidence=["candidate_profile:abc", "boss_job_snapshot:job-1"],
    )
    salary = FitCap(
        type="salary_below_expectation",
        reason="模型提议薪资冲突。",
        evidence=["candidate_profile:abc", "boss_job_snapshot:job-1"],
    )

    valid, rejected = JobFitEvaluator._validate_hard_caps(
        [schedule, salary],
        preferences={
            "internship_time": "每周 4-5 天，3-6 个月",
            "salary": "150-200元/天",
        },
        jobs=[
            {
                "experience": "3天/周 6个月",
                "salary": "100-200元/天",
            }
        ],
    )

    assert valid == []
    assert {item["type"] for item in rejected} == {
        "internship_duration_mismatch",
        "salary_below_expectation",
    }


def test_policy_derives_city_cap_and_corrects_place_time_explanation() -> None:
    profile_ref = "candidate_profile:abc"
    dimensions = [
        FitDimension(
            name=name,
            score=80,
            reason="模型原始解释。",
            evidence=[profile_ref, "boss_job_snapshot:job-1"],
        )
        for name in (
            "目标方向",
            "核心技能",
            "项目经历",
            "Agent/LLM 相关性",
            "地点与实习时间",
            "薪资与风险",
        )
    ]
    normalized, adjustments = JobFitEvaluator._normalize_policy_dimensions(
        JobFitDecision(
            status="ok",
            summary="测试",
            dimensions=dimensions,
            confidence=0.9,
        ),
        preferences={
            "cities": "杭州, 上海",
            "internship_time": "每周 4-5 天，3-6 个月",
            "salary": "150-200元/天",
        },
        jobs=[
            {
                "title": "Agent 开发实习生",
                "city": "无锡",
                "experience": "3天/周 6个月",
                "salary": "100-200元/天",
            }
        ],
    )
    place_time = next(item for item in normalized.dimensions if item.name == "地点与实习时间")
    salary = next(item for item in normalized.dimensions if item.name == "薪资与风险")
    valid, _ = JobFitEvaluator._validate_hard_caps(
        [],
        preferences={
            "cities": "杭州, 上海",
            "internship_time": "每周 4-5 天，3-6 个月",
        },
        jobs=[{"city": "无锡", "experience": "3天/周 6个月"}],
        evidence_refs=[profile_ref, "boss_job_snapshot:job-1"],
    )

    assert normalized.status == "ok"
    assert place_time.score == 50
    assert "地点不匹配" in place_time.reason
    assert "实习频次与周期匹配" in place_time.reason
    assert salary.score == 100
    assert "薪资区间匹配" in salary.reason
    assert adjustments
    assert [cap.type for cap in valid] == ["city_mismatch"]


def test_apply_skills_are_versioned_and_loaded_on_demand() -> None:
    library = ApplySkillLibrary()
    catalog = library.discover("opportunity")

    assert {item["name"] for item in catalog} == set(library.SCOPE_NAMES["opportunity"])
    assert {item["name"] for item in catalog} == {
        "grounded-candidate-communication",
        "opportunity-due-diligence",
        "interview-preparation",
    }
    loaded = library.load(
        "grounded-candidate-communication",
        scope="opportunity",
    )
    assert loaded["content_hash"]
    assert "grounded-candidate-communication" in loaded["path"]
    assert loaded["path"].endswith("SKILL.md")
    assert "有依据的候选人回复" in loaded["content"]
    assert loaded["tool_hints"] == [
        "memory_read",
        "profile_read",
        "job_read",
        "boss_fetch_job_detail",
    ]

    fit_catalog = library.discover("fit")
    assert fit_catalog == []


@pytest.mark.asyncio
async def test_job_assistant_message_cannot_be_used_as_job_fit_evidence() -> None:
    class PlatformAssistantStore(FakeStore):
        def opportunity_context(self, opportunity_id: str) -> dict[str, Any]:
            context = super().opportunity_context(opportunity_id)
            context["job_snapshots"] = []
            context["jobs"] = []
            context["messages"] = [
                {
                    "message_id": "assistant-message",
                    "sender_name": "求职助手",
                    "from_me": False,
                    "is_human_message": 1,
                    "message_type": "text",
                    "text": "我是你的求职助手，可以根据收藏岗位推送相似岗位。",
                }
            ]
            return context

    class NeverCalledModel(FakeFitModel):
        async def complete_json(self, *args: Any, **kwargs: Any) -> ModelTurn:
            pytest.fail("平台助手消息不能触发岗位契合度模型调用")

    result = await JobFitEvaluator(
        PlatformAssistantStore(),
        model=NeverCalledModel(),
    ).evaluate("opp-1")

    assert result["status"] == "needs_review"
    assert result["job_fit_score"] is None
    assert result["confidence"] == 0.0
