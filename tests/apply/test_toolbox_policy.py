from __future__ import annotations

from typing import Any

import pytest

from capybot.apply.agent_runtime.bootstrap import BootstrapContext
from capybot.apply.agent_runtime.mcp_client import MCPTool
from capybot.apply.agent_runtime.tools import ApplyToolbox


@pytest.mark.asyncio
async def test_toolbox_hides_context_tools_that_cannot_add_information() -> None:
    toolbox = ApplyToolbox(
        store=_UnusedStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=_EmptyMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:m1"},
        metadata={
            "memory_layers": [],
            "job_read_enabled": False,
            "profile_read_enabled": False,
            "skill_tools": [],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        names = {tool.name for tool in toolbox.tools}

    assert "memory_read" not in names
    assert "job_read" not in names
    assert "profile_read" not in names
    assert "fit_read" not in names
    assert "fit_recalculate" not in names
    assert not any(name.startswith("skill_") for name in names)


@pytest.mark.asyncio
async def test_toolbox_discloses_skill_contract_before_loading_body() -> None:
    toolbox = ApplyToolbox(
        store=_UnusedStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=_EmptyMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:m1"},
        metadata={
            "memory_layers": ["l2"],
            "job_read_enabled": True,
            "profile_read_enabled": False,
            "skill_tools": ["skill_grounded_candidate_communication"],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        initial = {tool.name: tool for tool in toolbox.tools}
        memory_schema = initial["memory_read"].input_schema

        assert memory_schema["properties"]["layer"]["enum"] == ["l2"]
        assert "query" not in memory_schema["properties"]
        assert "job_read" in initial
        skill_tool = initial["skill_grounded_candidate_communication"]
        assert "HR 追问候选人的项目" in skill_tool.description
        assert skill_tool.input_schema["properties"] == {}
        observation = await toolbox.call(
            "skill_grounded_candidate_communication",
            {},
        )
        assert observation["ok"] is True
        assert observation["facts"][0]["name"] == "grounded-candidate-communication"
        assert observation["facts"][0]["content"]
        assert observation["freshness"].startswith("sha256:")
        assert "skill_grounded_candidate_communication" not in {tool.name for tool in toolbox.tools}
        with pytest.raises(ValueError, match="不需要读取 l1"):
            await toolbox.call("memory_read", {"layer": "l1"})

        assert toolbox.normalize_arguments(
            "memory_read",
            {"layer": "l1", "limit": 5},
        ) == {"layer": "l2", "limit": 5}


@pytest.mark.asyncio
async def test_due_diligence_skill_unlocks_only_its_evidence_tools() -> None:
    client = _DueDiligenceMCPClient()
    toolbox = ApplyToolbox(
        store=_DueDiligenceStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=client,  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:new"},
        metadata={
            "memory_layers": [],
            "job_read_enabled": False,
            "profile_read_enabled": False,
            "skill_tools": ["skill_opportunity_due_diligence"],
            "external_tools": [],
            "external_tools_allowed": True,
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        before = {tool.name for tool in toolbox.tools}
        assert "boss_fetch_job_detail" not in before
        assert "research_company" not in before

        observation = await toolbox.call("skill_opportunity_due_diligence", {})
        after = {tool.name for tool in toolbox.tools}

    assert set(observation["facts"][0]["available_tools"]) == {
        "boss_fetch_job_detail",
        "job_read",
        "memory_read",
        "research_company",
    }
    assert {"boss_fetch_job_detail", "job_read", "memory_read", "research_company"} <= after
    assert "profile_read" not in after


@pytest.mark.asyncio
async def test_skill_cannot_bypass_disabled_external_tools() -> None:
    toolbox = ApplyToolbox(
        store=_DueDiligenceStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=_DueDiligenceMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:new"},
        metadata={
            "memory_layers": [],
            "job_read_enabled": False,
            "profile_read_enabled": False,
            "skill_tools": ["skill_opportunity_due_diligence"],
            "external_tools": [],
            "external_tools_allowed": False,
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        await toolbox.call("skill_opportunity_due_diligence", {})
        names = {tool.name for tool in toolbox.tools}

    assert "job_read" in names
    assert "memory_read" in names
    assert "boss_fetch_job_detail" not in names
    assert "research_company" not in names


@pytest.mark.asyncio
async def test_memory_read_returns_only_history_hidden_from_bootstrap() -> None:
    toolbox = ApplyToolbox(
        store=_HistoryStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=_EmptyMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:new"},
        metadata={
            "memory_layers": ["l1"],
            "job_read_enabled": False,
            "skill_tools": [],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        observation = await toolbox.call("memory_read", {"layer": "l1"})

    assert observation["evidence_refs"] == ["boss_message:old"]
    assert observation["facts"][0]["content"] == "此前已经发送过项目介绍。"


@pytest.mark.asyncio
async def test_profile_read_is_disclosed_only_for_grounded_personalized_reply() -> None:
    toolbox = ApplyToolbox(
        store=_ProfileStore(),  # type: ignore[arg-type]
        opportunity_id="opp-1",
        mcp_client=_EmptyMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs={"boss_message:new"},
        metadata={
            "memory_layers": [],
            "job_read_enabled": False,
            "profile_read_enabled": True,
            "skill_tools": [],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        assert "profile_read" in {tool.name for tool in toolbox.tools}
        observation = await toolbox.call("profile_read", {})
        assert "profile_read" not in {tool.name for tool in toolbox.tools}

    assert observation["ok"] is True
    assert observation["evidence_refs"][0].startswith("candidate_profile:")
    assert "Capybot Apply" in observation["facts"][0]["summary"]
    assert "resume_markdown" not in observation["facts"][0]


@pytest.mark.asyncio
async def test_toolbox_binds_mcp_calls_to_current_opportunity() -> None:
    client = _RecordingMCPClient()
    toolbox = ApplyToolbox(
        store=_UnusedStore(),  # type: ignore[arg-type]
        opportunity_id="opp-current",
        mcp_client=client,  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs=set(),
        metadata={
            "memory_layers": [],
            "job_read_enabled": False,
            "skill_tools": [],
            "external_tools": ["research_company"],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        tool = next(item for item in toolbox.tools if item.name == "research_company")
        assert "opportunity_id" not in tool.input_schema["properties"]
        assert "opportunity_id" not in tool.input_schema["required"]

        await toolbox.call(
            "research_company",
            {"focus": "business", "opportunity_id": "opp-model-controlled"},
        )

    assert client.calls == [
        (
            "research_company",
            {"focus": "business", "opportunity_id": "opp-current"},
        )
    ]


@pytest.mark.asyncio
async def test_successful_job_detail_mcp_hides_redundant_local_job_read() -> None:
    toolbox = ApplyToolbox(
        store=_UnusedStore(),  # type: ignore[arg-type]
        opportunity_id="opp-current",
        mcp_client=_JobDetailMCPClient(),  # type: ignore[arg-type]
    )
    bootstrap = BootstrapContext(
        prompt={},
        evidence_refs=set(),
        metadata={
            "memory_layers": [],
            "job_read_enabled": True,
            "skill_tools": [],
            "external_tools": ["boss_fetch_job_detail"],
        },
    )

    async with toolbox:
        toolbox.configure(bootstrap)
        assert "job_read" in {tool.name for tool in toolbox.tools}
        await toolbox.call("boss_fetch_job_detail", {})
        assert "job_read" not in {tool.name for tool in toolbox.tools}


class _UnusedStore:
    def opportunity_context(self, _opportunity_id: str) -> dict[str, Any]:
        return {"events": []}


class _HistoryStore:
    def opportunity_context(self, _opportunity_id: str) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "message_id": "old",
                    "from_me": True,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "此前已经发送过项目介绍。",
                },
                {
                    "message_id": "new",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "可以发一份简历吗？",
                },
            ],
            "events": [],
        }


class _ProfileStore:
    def opportunity_context(self, _opportunity_id: str) -> dict[str, Any]:
        return {
            "candidate_profile": {
                "profile_summary": "使用 Python 开发 Capybot Apply Tool-Calling Agent。",
                "skill_tags": ["Python", "MCP"],
                "project_tags": ["Capybot Apply"],
                "agent_tags": ["Tool Calling"],
                "updated_at": "2026-07-28T00:00:00+00:00",
            }
        }


class _DueDiligenceStore:
    def opportunity_context(self, _opportunity_id: str) -> dict[str, Any]:
        return {
            "opportunity": {"id": "opp-1", "company": "示例科技"},
            "messages": [
                {
                    "message_id": "old",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "这个岗位也会接触一些 AI 业务。",
                },
                {
                    "message_id": "new",
                    "from_me": False,
                    "message_type": "text",
                    "is_human_message": 1,
                    "text": "具体工作面试再聊。",
                },
            ],
            "events": [{"event_type": "risk_detected"}],
            "job_snapshots": [{"id": "job-1", "title": "AI 实习生"}],
        }


class _EmptyMCPClient:
    tools: list[Any] = []

    async def __aenter__(self) -> "_EmptyMCPClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def server_for(self, _name: str) -> str | None:
        return None

    async def call(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("MCP should not be called")


class _RecordingMCPClient(_EmptyMCPClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tools = [
            MCPTool(
                name="research_company",
                description="核验当前机会公司",
                input_schema={
                    "type": "object",
                    "properties": {
                        "opportunity_id": {"type": "string"},
                        "focus": {
                            "type": "string",
                            "enum": ["basic", "business", "employment"],
                        },
                    },
                    "required": ["opportunity_id"],
                    "additionalProperties": False,
                },
                server="company-intel",
            )
        ]

    def server_for(self, _name: str) -> str | None:
        return "company-intel"

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"ok": True, "summary": "done"}


class _JobDetailMCPClient(_RecordingMCPClient):
    def __init__(self) -> None:
        self.calls = []
        self.tools = [
            MCPTool(
                name="boss_fetch_job_detail",
                description="读取岗位详情",
                input_schema={
                    "type": "object",
                    "properties": {"opportunity_id": {"type": "string"}},
                    "required": ["opportunity_id"],
                    "additionalProperties": False,
                },
                server="boss",
            )
        ]

    def server_for(self, _name: str) -> str | None:
        return "boss"

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {
            "ok": True,
            "summary": "已读取岗位详情",
            "facts": [{"title": "Agent 实习"}],
            "evidence_refs": ["boss_job_snapshot:s1"],
        }


class _DueDiligenceMCPClient(_RecordingMCPClient):
    def __init__(self) -> None:
        self.calls = []
        self.tools = [
            MCPTool(
                name="boss_fetch_job_detail",
                description="读取岗位详情",
                input_schema={
                    "type": "object",
                    "properties": {"opportunity_id": {"type": "string"}},
                    "required": ["opportunity_id"],
                    "additionalProperties": False,
                },
                server="boss",
            ),
            MCPTool(
                name="research_company",
                description="核验公司背景",
                input_schema={
                    "type": "object",
                    "properties": {
                        "opportunity_id": {"type": "string"},
                        "focus": {
                            "type": "string",
                            "enum": ["basic", "business", "employment"],
                        },
                    },
                    "required": ["opportunity_id"],
                    "additionalProperties": False,
                },
                server="company-intel",
            ),
        ]

    def server_for(self, name: str) -> str | None:
        return "boss" if name == "boss_fetch_job_detail" else "company-intel"
