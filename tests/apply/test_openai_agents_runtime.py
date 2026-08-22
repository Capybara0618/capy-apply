from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from capybot.apply.agent_runtime.bootstrap import BootstrapContext
from capybot.apply.agent_runtime.model import ModelTurn, ToolCall
from capybot.apply.agent_runtime.sdk_runtime import OpenAIAgentsLoop
from capybot.apply.agent_runtime.tools import AgentTool


def _decision(evidence_ref: str) -> dict[str, Any]:
    return {
        "status": "ready",
        "stage": "need_my_action",
        "summary": "HR 已请求简历，需要候选人处理。",
        "next": {
            "action": "send_material",
            "owner": "me",
            "when": "today",
            "reason": "HR 正在等待简历。",
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
            {"kind": "task", "content": "发送简历", "evidence": [evidence_ref]},
            {
                "kind": "draft",
                "content": "您好，简历已附上，请查收。",
                "evidence": [evidence_ref],
            },
        ],
        "confidence": 0.9,
    }


class FakeBootstrapBuilder:
    def __init__(
        self,
        *,
        required_tools: list[str] | None = None,
        evidence_refs: set[str] | None = None,
    ) -> None:
        self.required_tools = (
            ["boss_fetch_job_detail"]
            if required_tools is None
            else required_tools
        )
        self.evidence_refs = set(evidence_refs or set())

    def build(
        self,
        opportunity_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> BootstrapContext:
        return BootstrapContext(
            prompt={
                "goal": "判断新增消息并按需补查岗位证据",
                "opportunity": {"id": opportunity_id, "stage": "communicating"},
                "delta": {"messages": []},
            },
            evidence_refs=self.evidence_refs,
            metadata={"required_any_tools": self.required_tools},
        )


class FakePlannerModel:
    provider_label = "fake"
    model_label = "fake-model"

    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.tool_names_history: list[set[str]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        self.calls += 1
        self.tool_names_history.append(
            {
                str((tool.get("function") or {}).get("name"))
                for tool in tools
            }
        )
        return self.turns.pop(0)


@dataclass
class FakeToolbox:
    calls: list[tuple[str, dict[str, Any]]]
    evidence_ref: str

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
                name="boss_fetch_job_detail",
                description="补取当前机会的BOSS岗位详情",
                input_schema={
                    "type": "object",
                    "properties": {"opportunity_id": {"type": "string"}},
                    "required": ["opportunity_id"],
                    "additionalProperties": False,
                },
                kind="mcp",
                handler=unused,
            )
        ]

    def kind_for(self, _name: str) -> str:
        return "mcp"

    def server_for(self, _name: str) -> str:
        return "boss"

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {
            "ok": True,
            "summary": "读取到岗位详情。",
            "facts": [{"title": "Agent开发实习生"}],
            "evidence_refs": [self.evidence_ref],
            "freshness": "boss_live",
        }


@pytest.mark.asyncio
async def test_sdk_runner_replans_after_tool_observation() -> None:
    evidence_ref = "boss_job_snapshot:job-1"
    model = FakePlannerModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="boss_fetch_job_detail",
                        arguments={"opportunity_id": "opp-1"},
                    )
                ],
            ),
            ModelTurn(
                content=json.dumps(_decision(evidence_ref), ensure_ascii=False),
            ),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    loop = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(calls, evidence_ref),
    )

    result, metrics = await loop.run("opp-1")

    assert result.accepted is True
    assert model.calls == 2
    assert calls == [
        ("boss_fetch_job_detail", {"opportunity_id": "opp-1"})
    ]
    assert metrics["loop_engine"] == "openai_agents_sdk"
    assert metrics["tool_call_count"] == 1
    assert metrics["used_tool_evidence_count"] == 1


@pytest.mark.asyncio
async def test_sdk_runner_enforces_required_tool_before_final() -> None:
    evidence_ref = "boss_job_snapshot:job-2"
    model = FakePlannerModel(
        [
            ModelTurn(content=json.dumps(_decision(evidence_ref), ensure_ascii=False)),
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="boss_fetch_job_detail",
                        arguments={"opportunity_id": "opp-2"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(_decision(evidence_ref), ensure_ascii=False)),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    traces: list[str] = []
    loop = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(calls, evidence_ref),
    )

    result, metrics = await loop.run(
        "opp-2",
        trace=lambda step_type, _title, _metadata: traces.append(step_type),
    )

    assert result.accepted is True
    assert model.calls == 3
    assert metrics["final_repair_count"] == 1
    assert "planner_guard" in traces


@pytest.mark.asyncio
async def test_sdk_runner_repairs_commit_gate_rejection() -> None:
    evidence_ref = "boss_message:m1"
    invalid = _decision(evidence_ref)
    invalid["suggestions"][1]["evidence"] = []
    model = FakePlannerModel(
        [
            ModelTurn(content=json.dumps(invalid, ensure_ascii=False)),
            ModelTurn(content=json.dumps(_decision(evidence_ref), ensure_ascii=False)),
        ]
    )
    loop = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(
            required_tools=[],
            evidence_refs={evidence_ref},
        ),
        toolbox_factory=lambda _opportunity_id: FakeToolbox([], evidence_ref),
    )

    result, metrics = await loop.run("opp-3")

    assert result.accepted is True
    assert model.calls == 2
    assert metrics["final_repair_count"] == 1


@pytest.mark.asyncio
async def test_sdk_runner_repairs_unused_external_evidence() -> None:
    initial_ref = "boss_message:m1"
    external_ref = "boss_job_snapshot:job-3"
    model = FakePlannerModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        name="boss_fetch_job_detail",
                        arguments={"opportunity_id": "opp-4"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(_decision(initial_ref), ensure_ascii=False)),
            ModelTurn(content=json.dumps(_decision(external_ref), ensure_ascii=False)),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    loop = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(evidence_refs={initial_ref}),
        toolbox_factory=lambda _opportunity_id: FakeToolbox(calls, external_ref),
    )

    result, metrics = await loop.run("opp-4")

    assert result.accepted is True
    assert model.calls == 3
    assert metrics["final_repair_count"] == 1
    assert metrics["used_tool_evidence_count"] == 1


@pytest.mark.asyncio
async def test_sdk_runner_discloses_tools_unlocked_by_skill_on_next_turn() -> None:
    evidence_ref = "boss_job_snapshot:job-4"

    class ProgressiveToolbox(FakeToolbox):
        skill_available = True
        job_unlocked = False

        @property
        def all_tools(self) -> list[AgentTool]:
            async def unused(_arguments: dict[str, Any]) -> dict[str, Any]:
                return {}

            return [
                AgentTool(
                    name="skill_opportunity_due_diligence",
                    description="加载岗位尽调方法",
                    input_schema={"type": "object", "properties": {}},
                    kind="skill",
                    handler=unused,
                ),
                *super().tools,
            ]

        @property
        def tools(self) -> list[AgentTool]:
            return [
                tool
                for tool in self.all_tools
                if (
                    (tool.kind == "skill" and self.skill_available)
                    or (tool.name == "boss_fetch_job_detail" and self.job_unlocked)
                )
            ]

        def kind_for(self, name: str) -> str:
            return "skill" if name.startswith("skill_") else "mcp"

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            if name.startswith("skill_"):
                self.skill_available = False
                self.job_unlocked = True
                return {
                    "ok": True,
                    "summary": "已加载岗位尽调规则并解锁岗位补查。",
                    "facts": [{"method": "先核对岗位详情，再判断缺口"}],
                    "evidence_refs": [],
                    "freshness": "sha256:test",
                }
            return await super().call(name, arguments)

    model = FakePlannerModel(
        [
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="skill-1",
                        name="skill_opportunity_due_diligence",
                        arguments={},
                    )
                ],
            ),
            ModelTurn(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="job-1",
                        name="boss_fetch_job_detail",
                        arguments={"opportunity_id": "opp-5"},
                    )
                ],
            ),
            ModelTurn(content=json.dumps(_decision(evidence_ref), ensure_ascii=False)),
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    loop = OpenAIAgentsLoop(
        model=model,
        bootstrap_builder=FakeBootstrapBuilder(required_tools=[]),
        toolbox_factory=lambda _opportunity_id: ProgressiveToolbox(calls, evidence_ref),
    )

    result, metrics = await loop.run("opp-5")

    assert result.accepted is True
    assert model.tool_names_history[0] == {"skill_opportunity_due_diligence"}
    assert model.tool_names_history[1] == {"boss_fetch_job_detail"}
    assert metrics["tool_call_count"] == 2
    assert metrics["skill_tool_call_count"] == 1
