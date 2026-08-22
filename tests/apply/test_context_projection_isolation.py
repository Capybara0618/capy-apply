from capybot.apply.agent_runtime.bootstrap import OpportunityBootstrapBuilder


class FakeStore:
    def opportunity_context(self, _opportunity_id):
        return {
            "opportunity": {
                "id": "opp-1",
                "title": "Agent 实习",
                "company": "示例科技",
                "stage": "communicating",
                "summary": "旧摘要",
                "next_action": "等待回复",
            },
            "messages": [
                {
                    "message_id": "m1",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "请发简历",
                }
            ],
            "events": [{"title": "不应预装的旧事件"}],
            "jobs": [
                {
                    "platform_job_id": "encrypted-job-1",
                    "raw_payload": {
                        "jobId": "encrypted-job-1",
                        "url": (
                            "bosszp://bosszhipin.app/openwith?type=jobview&securityId=security-1"
                        ),
                    },
                }
            ],
            "candidate_profile": {"resume_markdown": "不应预装的简历"},
            "fit_analysis": {"job_fit_score": 90},
        }


def test_bootstrap_exposes_only_goal_opportunity_and_delta() -> None:
    bootstrap = OpportunityBootstrapBuilder(FakeStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert set(bootstrap.prompt) == {
        "goal",
        "decision_scope",
        "opportunity",
        "delta",
    }
    serialized = str(bootstrap.prompt)
    assert "不应预装的旧事件" not in serialized
    assert "不应预装的简历" not in serialized
    assert "job_fit_score" not in serialized
    assert bootstrap.evidence_refs == {"boss_message:m1"}
    assert bootstrap.metadata["memory_layers"] == ["l2"]
    assert bootstrap.metadata["external_tools"] == []
    assert bootstrap.metadata["job_read_enabled"] is False
    assert bootstrap.metadata["profile_read_enabled"] is False
    assert bootstrap.metadata["skill_tools"] == [
        "skill_grounded_candidate_communication",
        "skill_interview_preparation",
        "skill_opportunity_due_diligence",
    ]
    assert bootstrap.metadata["pending_material_request_refs"] == ["boss_message:m1"]
    assert bootstrap.prompt["opportunity"]["evidence_state"] == {
        "hidden_history_count": 0,
        "event_count": 1,
        "has_job_evidence": True,
        "has_job_detail": False,
    }
    assert bootstrap.prompt["decision_scope"]["information_gaps"] == []


def test_research_trigger_changes_goal_without_expanding_context_shape() -> None:
    bootstrap = OpportunityBootstrapBuilder(FakeStore()).build(
        "opp-1",
        trigger={"type": "research"},
    )

    assert set(bootstrap.prompt) == {
        "goal",
        "decision_scope",
        "opportunity",
        "delta",
    }
    assert "BOSS 补全当前机会的岗位详情" in bootstrap.prompt["goal"]
    assert bootstrap.metadata["external_tools"] == ["boss_fetch_job_detail"]
    assert bootstrap.metadata["required_any_tools"] == ["boss_fetch_job_detail"]
    assert bootstrap.metadata["job_read_enabled"] is True


def test_research_progressively_discloses_company_after_job_detail_exists() -> None:
    class DetailedJobStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["job_snapshots"] = [
                {
                    "platform_job_id": "encrypted-job-1",
                    "payload": {
                        "platform_job_id": "encrypted-job-1",
                        "description": "负责 Agent 工具调用与评估。",
                    },
                }
            ]
            return context

    bootstrap = OpportunityBootstrapBuilder(DetailedJobStore()).build(
        "opp-1",
        trigger={"type": "research"},
    )

    assert "公司公开信息" in bootstrap.prompt["goal"]
    assert bootstrap.metadata["external_tools"] == ["research_company"]
    assert bootstrap.metadata["required_any_tools"] == ["research_company"]
    assert bootstrap.metadata["has_job_detail"] is True


def test_research_focus_can_request_company_without_job_detail() -> None:
    bootstrap = OpportunityBootstrapBuilder(FakeStore()).build(
        "opp-1",
        trigger={"type": "research", "focus": "company"},
    )

    assert "公司公开信息" in bootstrap.prompt["goal"]
    assert bootstrap.metadata["external_tools"] == ["research_company"]
    assert bootstrap.metadata["required_any_tools"] == ["research_company"]


def test_incremental_bootstrap_exposes_memory_only_for_unseen_history() -> None:
    class HistoryStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"] = [
                {
                    "message_id": "old",
                    "from_me": True,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "您好，我对岗位很感兴趣。",
                },
                *context["messages"],
            ]
            context["events"] = []
            return context

    bootstrap = OpportunityBootstrapBuilder(HistoryStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert bootstrap.metadata["memory_layers"] == ["l1"]
    assert bootstrap.prompt["opportunity"]["evidence_state"]["hidden_history_count"] == 1
    assert bootstrap.evidence_refs == {"boss_message:m1"}


def test_risk_delta_uses_existing_evidence_without_generic_risk_mcp() -> None:
    class RiskStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"][0]["text"] = "入职前需要先缴纳培训费。"
            return context

    bootstrap = OpportunityBootstrapBuilder(RiskStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert bootstrap.metadata["external_tools"] == []
    assert bootstrap.metadata["suggested_external_tools"] == []


def test_project_question_marks_job_detail_as_planner_information_gap() -> None:
    class ProjectQuestionStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"][0]["text"] = "可以介绍一下你的 Agent 项目经历吗？"
            return context

    bootstrap = OpportunityBootstrapBuilder(ProjectQuestionStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert bootstrap.metadata["suggested_external_tools"] == ["boss_fetch_job_detail"]
    assert bootstrap.metadata["profile_read_enabled"] is True
    assert "profile_read" in bootstrap.metadata["required_any_tools"]
    assert bootstrap.prompt["decision_scope"]["planner_must_decide"] is True
    assert any("完整岗位要求" in gap for gap in bootstrap.metadata["information_gaps"])


def test_interview_invitation_exposes_skill_catalog_and_candidate_profile() -> None:
    class InterviewStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"][0]["text"] = "下周三下午两点安排线上技术面试，方便参加吗？"
            return context

    bootstrap = OpportunityBootstrapBuilder(InterviewStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert bootstrap.metadata["skill_tools"] == [
        "skill_grounded_candidate_communication",
        "skill_interview_preparation",
        "skill_opportunity_due_diligence",
    ]
    assert bootstrap.metadata["profile_read_enabled"] is True
    assert bootstrap.metadata["interview_signal_refs"] == ["boss_message:m1"]


def test_employment_identity_marks_company_research_as_information_gap() -> None:
    class EmploymentStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"][0]["text"] = "岗位由合作项目组招聘，入职主体后续确认，可以接受吗？"
            return context

    bootstrap = OpportunityBootstrapBuilder(EmploymentStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m1"]},
    )

    assert bootstrap.metadata["suggested_external_tools"] == ["research_company"]
    assert "公司主体" in bootstrap.metadata["information_gaps"][0]


def test_material_sent_after_hr_request_is_exposed_to_commit_gate() -> None:
    class MaterialSentStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"] = [
                {
                    "message_id": "request",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "方便把简历发来吗？",
                },
                {
                    "message_id": "attachment",
                    "from_me": True,
                    "message_type": "image",
                    "is_human_message": 1,
                    "text": None,
                },
                {
                    "message_id": "sent",
                    "from_me": True,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "简历已经发您了。",
                },
            ]
            return context

    bootstrap = OpportunityBootstrapBuilder(MaterialSentStore()).build(
        "opp-1",
        trigger={
            "type": "import_delta",
            "new_message_ids": ["request", "attachment", "sent"],
        },
    )

    assert bootstrap.metadata["material_completed_after_request"] is True
    assert (
        bootstrap.prompt["delta"]["conversation_state"]["material_completed_after_request"] is True
    )


def test_incremental_trigger_can_explicitly_disable_external_tools() -> None:
    bootstrap = OpportunityBootstrapBuilder(FakeStore()).build(
        "opp-1",
        trigger={
            "type": "import_delta",
            "new_message_ids": ["m1"],
            "allow_external": False,
        },
    )

    assert bootstrap.metadata["external_tools"] == []


def test_ambiguous_hr_invitation_preserves_evidence_for_agent_decision() -> None:
    class InvitationStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"][0]["text"] = "我们周六有一场 AI 沙龙，有兴趣过来听听吗？"
            return context

    bootstrap = OpportunityBootstrapBuilder(InvitationStore()).build(
        "opp-1",
        trigger={"type": "manual"},
    )

    assert "requires_skill" not in bootstrap.metadata
    assert bootstrap.metadata["pending_hr_question_refs"] == ["boss_message:m1"]
    assert bootstrap.prompt["delta"]["pending_hr_question_refs"] == ["boss_message:m1"]


def test_conditional_future_interview_is_not_treated_as_interview_invite() -> None:
    class ConditionalInterviewStore(FakeStore):
        def opportunity_context(self, opportunity_id):
            context = super().opportunity_context(opportunity_id)
            context["messages"] = [
                {
                    "message_id": "m0",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "我们本周有一场技术交流会，会介绍团队项目。",
                },
                {
                    "message_id": "m1",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "你有兴趣吗？后续是否进入面试需要再评估。",
                },
            ]
            return context

    bootstrap = OpportunityBootstrapBuilder(ConditionalInterviewStore()).build(
        "opp-1",
        trigger={"type": "import_delta", "new_message_ids": ["m0", "m1"]},
    )

    assert bootstrap.metadata["interview_signal_refs"] == []
    assert bootstrap.metadata["suggested_external_tools"] == []
    assert "requires_skill" not in bootstrap.metadata


def test_boss_refresh_trigger_exposes_only_boss_mcp() -> None:
    bootstrap = OpportunityBootstrapBuilder(FakeStore()).build(
        "opp-1",
        trigger={"type": "boss_refresh"},
    )

    assert bootstrap.metadata["external_tools"] == ["boss_refresh_opportunity"]
    assert bootstrap.metadata["required_any_tools"] == ["boss_refresh_opportunity"]
    assert "BOSS 证据" in bootstrap.prompt["goal"]
