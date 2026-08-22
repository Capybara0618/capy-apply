from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from capybot.apply.agent_runtime.bootstrap import BootstrapContext
from capybot.apply.agent_runtime.commit_gate import CommitGate
from capybot.apply.agent_runtime.model import ModelTurn, ToolCall
from capybot.apply.agent_runtime.sdk_runtime import OpenAIAgentsLoop
from capybot.apply.agent_runtime.tools import AgentTool


def decision(*, evidence_ref: str = "boss_message:m1") -> dict[str, Any]:
    return {
        "status": "ready",
        "stage": "need_my_action",
        "summary": "HR 已请求简历，当前需要候选人行动。",
        "next": {
            "action": "send_material",
            "owner": "me",
            "when": "now",
            "reason": "HR 请求补充简历。",
            "evidence": [evidence_ref],
        },
        "changes": [
            {
                "type": "material_requested",
                "detail": "HR 请求发送简历。",
                "evidence": [evidence_ref],
            }
        ],
        "suggestions": [
            {
                "kind": "task",
                "content": "发送简历",
                "evidence": [evidence_ref],
            },
            {
                "kind": "draft",
                "content": "您好，简历已附上，请查收。",
                "evidence": [evidence_ref],
            },
        ],
        "confidence": 0.9,
    }


class FakeBootstrapBuilder:
    def build(
        self,
        opportunity_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> BootstrapContext:
        trigger_type = str((trigger or {}).get("type") or "")
        required_tools = {
            "boss_refresh": ["boss_refresh_opportunity"],
            "research": ["boss_fetch_job_detail"],
        }.get(trigger_type, [])
        return BootstrapContext(
            prompt={
                "goal": "判断本次新增消息如何影响当前求职机会",
                "opportunity": {
                    "id": opportunity_id,
                    "stage": "communicating",
                    "summary": "正在沟通",
                },
                "delta": {
                    "count": 1,
                    "messages": [
                        {
                            "ref": "boss_message:m1",
                            "speaker": "hr",
                            "type": "text",
                            "content": "方便发一份简历吗？",
                            "sent_at": None,
                        }
                    ],
                    "all_refs": ["boss_message:m1"],
                    "truncated": False,
                },
            },
            evidence_refs={"boss_message:m1"},
            metadata={
                "delta_count": 1,
                "required_any_tools": required_tools,
            },
        )


class FakeModel:
    provider_label = "fake"
    model_label = "fake-model"

    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.message_history: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        self.calls += 1
        self.message_history.append(list(messages))
        return self.turns.pop(0)


@dataclass
class FakeToolbox:
    result: dict[str, Any]
    calls: list[tuple[str, dict[str, Any]]]

    async def __aenter__(self) -> "FakeToolbox":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    @property
    def tools(self) -> list[AgentTool]:
        async def unused(_arguments: dict[str, Any]) -> dict[str, Any]:
            return {}

        return [
            AgentTool(
                name="boss_refresh_opportunity",
                description="refresh BOSS evidence",
                input_schema={
                    "type": "object",
                    "properties": {"opportunity_id": {"type": "string"}},
                    "required": ["opportunity_id"],
                },
                kind="mcp",
                handler=unused,
            ),
            AgentTool(
                name="boss_fetch_job_detail",
                description="fetch BOSS job detail",
                input_schema={
                    "type": "object",
                    "properties": {"opportunity_id": {"type": "string"}},
                    "required": ["opportunity_id"],
                },
                kind="mcp",
                handler=unused,
            ),
            AgentTool(
                name="memory_read",
                description="read opportunity memory",
                input_schema={
                    "type": "object",
                    "properties": {
                        "layer": {"type": "string", "enum": ["l1", "l2"]},
                        "limit": {"type": "integer"},
                    },
                    "required": ["layer"],
                },
                kind="memory",
                handler=unused,
            ),
        ]

    def kind_for(self, _name: str) -> str:
        return "mcp"

    def server_for(self, _name: str) -> str:
        return "boss"

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self.result


@pytest.mark.asyncio
async def test_agent_finishes_without_tool_when_delta_is_sufficient() -> None:
    model = FakeModel([ModelTurn(content=json.dumps(decision(), ensure_ascii=False))])
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox({}, tool_calls),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["tool_call_count"] == 0
    assert tool_calls == []
    assert model.calls == 1
    assert set(json.loads(model.message_history[0][1]["content"])) == {
        "goal",
        "opportunity",
        "delta",
    }


@pytest.mark.asyncio
async def test_agent_replans_after_real_tool_observation() -> None:
    external_ref = "boss_job_snapshot:snapshot-2"
    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="boss_refresh_opportunity",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(decision(evidence_ref=external_ref), ensure_ascii=False)),
        ]
    )
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {
                "ok": True,
                "summary": "岗位详情已刷新",
                "facts": [{"title": "Agent 开发实习生"}],
                "evidence_refs": [external_ref],
                "freshness": "boss_live_refresh",
            },
            tool_calls,
        ),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["tool_call_count"] == 1
    assert metrics["external_tool_call_count"] == 1
    assert metrics["novel_tool_evidence_count"] == 1
    assert metrics["used_tool_evidence_count"] == 1
    assert metrics["evidence_used_tool_call_count"] == 1
    assert metrics["empty_tool_call_count"] == 0
    assert model.calls == 2
    assert tool_calls == [
        ("boss_refresh_opportunity", {"opportunity_id": "opp-1"})
    ]


@pytest.mark.asyncio
async def test_agent_repairs_final_that_drops_external_mcp_evidence() -> None:
    external_ref = "boss_job_snapshot:snapshot-2"
    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="boss_refresh_opportunity",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
            ModelTurn(
                content=json.dumps(
                    decision(evidence_ref=external_ref),
                    ensure_ascii=False,
                )
            ),
        ]
    )
    traces: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {
                "ok": True,
                "summary": "岗位详情已刷新",
                "facts": [{"title": "Agent 开发实习生"}],
                "evidence_refs": [external_ref],
                "freshness": "boss_live_refresh",
            },
            [],
        ),
    )

    result, metrics = await agent.run(
        "opp-1",
        trace=lambda step_type, _title, metadata: traces.append(
            (step_type, metadata)
        ),
    )

    assert result.accepted is True
    assert metrics["final_repair_count"] == 1
    assert metrics["used_tool_evidence_count"] == 1
    assert any(step_type == "self_check" for step_type, _ in traces)


@pytest.mark.asyncio
async def test_explicit_refresh_cannot_finish_before_required_mcp_call() -> None:
    model = FakeModel(
        [
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-refresh",
                        name="boss_refresh_opportunity",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
        ]
    )
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    traces: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {
                "ok": True,
                "summary": "BOSS evidence refreshed",
                "facts": [{"message_id": "m1"}],
                "evidence_refs": ["boss_message:m1"],
                "freshness": "boss_live_refresh",
            },
            tool_calls,
        ),
    )

    result, metrics = await agent.run(
        "opp-1",
        trigger={"type": "boss_refresh"},
        trace=lambda step_type, _title, metadata: traces.append(
            (step_type, metadata)
        ),
    )

    assert result.accepted is True
    assert metrics["tool_call_count"] == 1
    assert tool_calls == [
        ("boss_refresh_opportunity", {"opportunity_id": "opp-1"})
    ]
    assert any(step_type == "planner_guard" for step_type, _ in traces)


@pytest.mark.asyncio
async def test_failed_required_mcp_keeps_original_state() -> None:
    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-refresh",
                        name="boss_refresh_opportunity",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            )
        ]
    )
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {
                "ok": False,
                "summary": "BOSS 实时刷新失败：登录态不可读",
                "facts": [],
                "evidence_refs": [],
                "freshness": "failed",
                "error": "登录态不可读",
            },
            [],
        ),
    )

    result, metrics = await agent.run(
        "opp-1",
        trigger={"type": "boss_refresh"},
    )

    assert result.accepted is False
    assert result.status == "needs_review"
    assert "保留原机会状态" in result.errors[0]
    assert metrics["tool_call_count"] == 1
    assert model.calls == 1


@pytest.mark.asyncio
async def test_empty_research_observation_cannot_satisfy_required_evidence() -> None:
    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-job-detail",
                        name="boss_fetch_job_detail",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            ),
        ]
    )
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {
                "ok": False,
                "summary": "岗位已下线，未返回详情",
                "facts": [
                    {
                        "kind": "job_detail_status",
                        "status": "needs_context",
                    }
                ],
                "evidence_refs": [],
                "freshness": "missing_job_identity",
            },
            [],
        ),
    )

    result, metrics = await agent.run(
        "opp-1",
        trigger={"type": "research"},
    )

    assert result.accepted is False
    assert "保留原机会状态" in result.errors[0]
    assert metrics["tool_call_count"] == 1
    assert model.calls == 1


def test_commit_gate_accepts_top_level_decision_evidence() -> None:
    payload = decision()
    payload["evidence"] = ["boss_message:m1"]

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.decision["evidence"] == ["boss_message:m1"]


def test_commit_gate_rejects_waiting_when_latest_hr_question_is_unanswered() -> None:
    payload = decision()
    payload["stage"] = "waiting_feedback"
    payload["next"] = {
        "action": "wait",
        "owner": "hr",
        "when": "none",
        "reason": "等待对方",
        "evidence": ["boss_message:m1"],
    }
    payload["suggestions"] = []

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        pending_hr_question_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert any("不得判定为继续等待" in error for error in result.errors)


def test_commit_gate_accepts_verify_for_pending_identity_risk() -> None:
    payload = decision()
    payload["next"] = {
        "action": "verify",
        "owner": "me",
        "when": "now",
        "reason": "需要确认实际入职主体",
        "evidence": ["boss_message:m1"],
    }
    payload["suggestions"] = [
        {
            "kind": "risk",
            "content": "合作项目招聘，入职主体尚未确认。",
            "severity": "medium",
            "evidence": ["boss_message:m1"],
        }
    ]

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        pending_hr_question_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.decision["next"]["action"] == "verify"


def test_commit_gate_normalizes_completed_material_to_waiting_feedback() -> None:
    payload = decision()
    payload["stage"] = "communicating"
    payload["next"] = {
        "action": "wait",
        "owner": "none",
        "when": "none",
        "reason": "材料已发送",
        "evidence": ["boss_message:m1"],
    }
    payload["suggestions"] = []

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        material_completed_after_request=True,
    )

    assert result.accepted is True
    assert result.decision["stage"] == "waiting_feedback"
    assert result.decision["next"]["owner"] == "hr"


def test_commit_gate_requires_send_material_for_unfinished_material_request() -> None:
    payload = decision()
    payload["next"] = {
        "action": "reply",
        "owner": "me",
        "when": "now",
        "reason": "回复 HR",
        "evidence": ["boss_message:m1"],
    }

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        pending_material_request_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert "send_material" in "；".join(result.errors)


def test_runtime_drops_legacy_next_action_field() -> None:
    payload = decision()
    payload["next_action"] = "wait"

    parsed = OpenAIAgentsLoop._parse_final(json.dumps(payload))

    assert "next_action" not in parsed
    assert parsed["next"]["action"] == "send_material"


def test_commit_gate_rejects_unknown_evidence() -> None:
    result = CommitGate().validate(
        decision(evidence_ref="boss_message:invented"),
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert "invented" in result.errors[0]


def test_commit_gate_rejects_tool_injected_evidence_namespace() -> None:
    result = CommitGate().validate(
        decision(evidence_ref="internal_db:row-1"),
        valid_evidence_refs={"internal_db:row-1"},
    )

    assert result.accepted is False
    assert "boss_message" in result.errors[0]


def test_commit_gate_rejects_risk_without_severity() -> None:
    payload = decision()
    payload["suggestions"].append(
        {
            "kind": "risk",
            "content": "岗位存在收费风险",
            "evidence": ["boss_message:m1"],
        }
    )

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert "requires severity" in result.errors[0]


def test_commit_gate_normalizes_explicit_payment_risk_to_verify() -> None:
    payload = decision()
    payload["suggestions"].append(
        {
            "kind": "risk",
            "content": "岗位要求支付培训费",
            "severity": "medium",
            "evidence": ["boss_message:m1"],
        }
    )

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        critical_risk_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.status == "needs_review"
    assert result.decision["next"]["action"] == "verify"
    risks = [item for item in result.decision["suggestions"] if item["kind"] == "risk"]
    assert risks[0]["severity"] == "high"
    assert risks[0]["evidence"] == ["boss_message:m1"]


def test_commit_gate_keeps_lifecycle_stage_separate_from_current_action() -> None:
    payload = decision()
    payload["stage"] = "interviewing"

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.decision["stage"] == "interviewing"
    assert result.decision["next"]["action"] == "send_material"


def test_commit_gate_derives_action_state_outside_interview() -> None:
    payload = decision()
    payload["stage"] = "communicating"

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.decision["stage"] == "need_my_action"


def test_commit_gate_preserves_interview_progress_from_hr_evidence() -> None:
    payload = decision()
    payload["stage"] = "communicating"

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
        interview_signal_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.decision["stage"] == "interviewing"


def test_commit_gate_strips_decorative_web_sources_from_reply() -> None:
    payload = decision()
    payload["next"]["evidence"].append("web_source:irrelevant")

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1", "web_source:irrelevant"},
    )

    assert result.accepted is True
    assert result.status == "needs_review"
    assert result.decision["next"]["evidence"] == ["boss_message:m1"]
    assert result.warnings


def test_commit_gate_rejects_web_only_evidence_on_reply() -> None:
    payload = decision(evidence_ref="web_source:irrelevant")

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"web_source:irrelevant"},
    )

    assert result.accepted is False


def test_commit_gate_reanchors_verify_task_to_triggering_hr_message() -> None:
    payload = decision()
    payload["next"] = {
        "action": "verify",
        "owner": "me",
        "when": "now",
        "reason": "招聘主体尚不明确。",
        "evidence": ["boss_message:m1"],
    }
    payload["changes"] = []
    payload["suggestions"][0]["content"] = "核实招聘主体"
    payload["suggestions"][0]["evidence"] = ["web_source:company"]

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1", "web_source:company"},
    )

    assert result.accepted is True
    assert result.status == "needs_review"
    assert result.decision["suggestions"][0]["evidence"] == ["boss_message:m1"]
    assert any("本地证据" in warning for warning in result.warnings)


def test_commit_gate_requires_actionable_task_and_draft() -> None:
    payload = decision()
    payload["suggestions"] = []

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert any("待确认任务" in error for error in result.errors)
    assert any("待确认草稿" in error for error in result.errors)


def test_commit_gate_requires_task_and_draft_for_interview_confirmation() -> None:
    payload = decision()
    payload["stage"] = "interviewing"
    payload["next"] = {
        "action": "confirm_interview",
        "owner": "me",
        "when": "now",
        "reason": "HR 给出了面试时间，需要候选人确认。",
        "evidence": ["boss_message:m1"],
    }
    payload["suggestions"] = []

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert any("待确认任务" in error for error in result.errors)
    assert any("待确认草稿" in error for error in result.errors)


def test_commit_gate_rejects_template_placeholders_in_draft() -> None:
    payload = decision()
    draft = next(item for item in payload["suggestions"] if item["kind"] == "draft")
    draft["content"] = "您好，我是[您的名字]，最近参与了[项目名称]。"

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is False
    assert any("模板占位符" in error for error in result.errors)


def test_commit_gate_accepts_null_next_only_when_evidence_is_insufficient() -> None:
    payload = {
        "status": "insufficient_evidence",
        "stage": "discovered",
        "summary": "当前没有足够证据判断下一步。",
        "next": None,
        "changes": [],
        "suggestions": [],
        "confidence": 0,
    }

    result = CommitGate().validate(payload, valid_evidence_refs=set())

    assert result.accepted is True
    assert result.status == "needs_review"
    assert result.decision["next"] is None

    payload["next"] = {}
    empty_object_result = CommitGate().validate(payload, valid_evidence_refs=set())
    assert empty_object_result.accepted is True
    assert empty_object_result.decision["next"] is None


def test_commit_gate_turns_evidenced_high_risk_into_verify_review() -> None:
    payload = {
        "status": "needs_review",
        "stage": "communicating",
        "summary": "对方要求先缴纳培训费。",
        "next": None,
        "changes": [],
        "suggestions": [
            {
                "kind": "risk",
                "content": "存在收费培训风险，需要先核实。",
                "severity": "high",
                "evidence": ["boss_message:m1"],
            }
        ],
        "confidence": 0.9,
    }

    result = CommitGate().validate(
        payload,
        valid_evidence_refs={"boss_message:m1"},
    )

    assert result.accepted is True
    assert result.status == "needs_review"
    assert result.decision["next"]["action"] == "verify"
    assert any("安全策略" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_agent_returns_duplicate_observation_and_continues() -> None:
    repeated = ToolCall(
        id="call-1",
        name="boss_refresh_opportunity",
        arguments={"opportunity_id": "opp-1"},
    )
    model = FakeModel(
        [
            ModelTurn(content=None, tool_calls=[repeated]),
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name=repeated.name,
                        arguments=repeated.arguments,
                    )
                ],
            ),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
        ]
    )
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {"ok": True, "evidence_refs": []},
            tool_calls,
        ),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["tool_call_count"] == 1
    assert metrics["duplicate_tool_call_count"] == 1
    assert len(tool_calls) == 1
    duplicate_observation = json.loads(model.message_history[2][-1]["content"])
    assert duplicate_observation["duplicate"] is True


@pytest.mark.asyncio
async def test_agent_deduplicates_memory_reads_by_layer() -> None:
    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-memory-1",
                        name="memory_read",
                        arguments={"layer": "l1", "limit": 10},
                    ),
                    ToolCall(
                        id="call-memory-2",
                        name="memory_read",
                        arguments={"layer": "l1", "limit": 20},
                    ),
                ],
            ),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
        ]
    )
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(
            {"ok": True, "evidence_refs": []},
            tool_calls,
        ),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["tool_call_count"] == 1
    assert metrics["duplicate_tool_call_count"] == 1
    assert len(tool_calls) == 1


@pytest.mark.asyncio
async def test_agent_normalizes_single_visible_memory_layer_before_deduplication() -> None:
    class NormalizingToolbox(FakeToolbox):
        def normalize_arguments(
            self,
            name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            if name == "memory_read":
                return {**arguments, "layer": "l1"}
            return arguments

    model = FakeModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-memory-l1",
                        name="memory_read",
                        arguments={"layer": "l1", "limit": 1},
                    ),
                    ToolCall(
                        id="call-memory-l2",
                        name="memory_read",
                        arguments={"layer": "l2", "limit": 1},
                    ),
                ],
            ),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
        ]
    )
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: NormalizingToolbox(
            {"ok": True, "evidence_refs": []},
            tool_calls,
        ),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["tool_call_count"] == 1
    assert metrics["duplicate_tool_call_count"] == 1
    assert metrics["tool_argument_normalization_count"] == 1
    assert tool_calls == [("memory_read", {"layer": "l1", "limit": 1})]


@pytest.mark.asyncio
async def test_agent_returns_commit_errors_for_one_model_repair() -> None:
    invalid = decision()
    invalid["suggestions"][1]["evidence"] = []
    model = FakeModel(
        [
            ModelTurn(content=json.dumps(invalid, ensure_ascii=False)),
            ModelTurn(content=json.dumps(decision(), ensure_ascii=False)),
        ]
    )
    agent = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox({}, []),
    )

    result, metrics = await agent.run("opp-1")

    assert result.accepted is True
    assert metrics["iterations"] == 2
    assert "CommitGate 拒绝了输出" in model.message_history[1][-1]["content"]
