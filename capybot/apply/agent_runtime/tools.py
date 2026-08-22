"""Typed local tools and allowlisted MCP tools for the Opportunity Agent."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from capybot.apply.store import ApplyStore

from .bootstrap import BootstrapContext, OpportunityBootstrapBuilder
from .mcp_client import MCPToolClient
from .skills import ApplySkillLibrary

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    kind: str
    handler: ToolHandler

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ApplyToolbox:
    """Expose local typed tools plus two genuine external MCP servers."""

    OPPORTUNITY_BOUND_MCP_TOOLS = frozenset(
        {
            "boss_refresh_opportunity",
            "boss_fetch_job_detail",
            "research_company",
        }
    )

    def __init__(
        self,
        *,
        store: ApplyStore,
        opportunity_id: str,
        mcp_client: MCPToolClient,
        skills: ApplySkillLibrary | None = None,
    ) -> None:
        self.store = store
        self.opportunity_id = opportunity_id
        self.mcp_client = mcp_client
        self.skills = skills or ApplySkillLibrary()
        self._tools: dict[str, AgentTool] = {}
        self._memory_layers: frozenset[str] = frozenset()
        self._visible_evidence_refs: frozenset[str] = frozenset()
        self._job_read_enabled = False
        self._profile_read_enabled = False
        self._enabled_skill_tools: frozenset[str] = frozenset()
        self._enabled_mcp_tools: frozenset[str] = frozenset()
        self._external_tools_allowed = True

    async def __aenter__(self) -> "ApplyToolbox":
        await self.mcp_client.__aenter__()
        self._register_local_tools()
        for tool in self.mcp_client.tools:
            self._register(
                AgentTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=self._agent_visible_mcp_schema(
                        tool.name,
                        tool.input_schema,
                    ),
                    kind="mcp",
                    handler=self._mcp_handler(tool.name),
                )
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.mcp_client.__aexit__(exc_type, exc, tb)
        self._tools.clear()

    @property
    def tools(self) -> list[AgentTool]:
        visible = []
        for tool in self._tools.values():
            if tool.name == "memory_read" and not self._memory_layers:
                continue
            if tool.name == "job_read" and not self._job_read_enabled:
                continue
            if tool.name == "profile_read" and not self._profile_read_enabled:
                continue
            if tool.kind == "skill" and tool.name not in self._enabled_skill_tools:
                continue
            if tool.kind == "mcp" and tool.name not in self._enabled_mcp_tools:
                continue
            if tool.name == "memory_read":
                schema = dict(tool.input_schema)
                properties = dict(schema.get("properties") or {})
                properties["layer"] = {
                    "type": "string",
                    "enum": sorted(self._memory_layers),
                }
                schema["properties"] = properties
                tool = replace(tool, input_schema=schema)
            visible.append(tool)
        return visible

    @property
    def all_tools(self) -> list[AgentTool]:
        """Return the registry; the SDK applies dynamic visibility each turn."""

        return list(self._tools.values())

    def configure(self, bootstrap: BootstrapContext) -> None:
        """Expose only tools that can add context not already in the bootstrap."""

        self._memory_layers = frozenset(
            str(layer)
            for layer in bootstrap.metadata.get("memory_layers") or []
            if layer in {"l1", "l2"}
        )
        self._visible_evidence_refs = frozenset(bootstrap.evidence_refs)
        self._job_read_enabled = bool(bootstrap.metadata.get("job_read_enabled"))
        self._profile_read_enabled = bool(bootstrap.metadata.get("profile_read_enabled"))
        self._enabled_skill_tools = frozenset(
            str(name) for name in bootstrap.metadata.get("skill_tools") or []
        )
        self._enabled_mcp_tools = frozenset(
            str(name) for name in bootstrap.metadata.get("external_tools") or []
        )
        self._external_tools_allowed = bool(bootstrap.metadata.get("external_tools_allowed", True))

    def kind_for(self, name: str) -> str | None:
        tool = self._tools.get(name)
        return tool.kind if tool else None

    def server_for(self, name: str) -> str | None:
        return self.mcp_client.server_for(name)

    def normalize_arguments(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize only arguments with a single safe interpretation."""

        normalized = dict(arguments)
        if name == "memory_read" and len(self._memory_layers) == 1:
            normalized["layer"] = next(iter(self._memory_layers))
        return normalized

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        visible = {tool.name: tool for tool in self.tools}
        tool = visible.get(name)
        if tool is None:
            raise PermissionError(f"工具不在 Opportunity Agent 白名单中: {name}")
        observation = self._normalize_observation(await tool.handler(arguments))
        if (
            name == "boss_fetch_job_detail"
            and observation.get("ok")
            and observation.get("evidence_refs")
        ):
            self._job_read_enabled = False
        if name == "profile_read" and observation.get("ok"):
            self._profile_read_enabled = False
        if tool.kind == "skill" and observation.get("ok"):
            self._enabled_skill_tools = frozenset(
                item for item in self._enabled_skill_tools if item != name
            )
        return observation

    def _register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise RuntimeError(f"Apply tool name collision: {tool.name}")
        self._tools[tool.name] = tool

    def _register_local_tools(self) -> None:
        self._register(
            AgentTool(
                name="memory_read",
                description=(
                    "读取当前机会尚未展示的最近 L1 消息或 L2 事件；每个记忆层只需调用一次。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "layer": {"type": "string", "enum": ["l1", "l2"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["layer"],
                    "additionalProperties": False,
                },
                kind="memory",
                handler=self._memory_read,
            )
        )
        self._register(
            AgentTool(
                name="job_read",
                description="读取当前机会的岗位卡和最新版本化岗位快照。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                kind="memory",
                handler=self._job_read,
            )
        )
        self._register(
            AgentTool(
                name="profile_read",
                description=(
                    "读取当前账号的脱敏候选人画像摘要、技能和项目标签；"
                    "仅用于 HR 要求自我介绍、项目经历或技术栈时生成有依据的回复。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                kind="memory",
                handler=self._profile_read,
            )
        )
        for descriptor in self.skills.discover("opportunity"):
            skill_name = str(descriptor["name"])
            self._register(
                AgentTool(
                    name=self.skills.tool_name(skill_name),
                    description=(
                        f"{descriptor['description']}"
                        " 调用后返回该领域 Skill 的完整规则，仅在规则会改变判断时使用。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    kind="skill",
                    handler=self._skill_handler(skill_name),
                )
            )

    async def _memory_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        context = self.store.opportunity_context(self.opportunity_id)
        layer = str(arguments.get("layer") or "")
        if layer not in self._memory_layers:
            raise ValueError(f"当前上下文不需要读取 {layer} 记忆层")
        limit = min(20, max(1, int(arguments.get("limit") or 10)))
        if layer == "l1":
            messages = [
                message
                for message in context.get("messages") or []
                if bool(message.get("is_human_message", 1))
                and str(message.get("message_type") or "text")
                not in {"platform_card", "system", "auto_followup"}
                and f"boss_message:{message.get('message_id')}" not in self._visible_evidence_refs
            ]
            selected = messages[-limit:]
            values = [OpportunityBootstrapBuilder._message(message) for message in selected]
            refs = [message["ref"] for message in values]
            return {
                "ok": True,
                "summary": f"读取到 {len(values)} 条 L1 人类消息。",
                "facts": values,
                "evidence_refs": refs,
                "freshness": "local_current",
            }
        if layer == "l2":
            events = list(context.get("events") or [])
            selected = events[-limit:]
            refs = self._event_refs(selected)
            return {
                "ok": True,
                "summary": f"读取到 {len(selected)} 条 L2 进展事件。",
                "facts": [
                    {
                        "type": event.get("event_type"),
                        "detail": event.get("detail") or event.get("title"),
                        "created_at": event.get("created_at"),
                    }
                    for event in selected
                ],
                "evidence_refs": refs,
                "freshness": "local_projection",
            }
        raise ValueError(f"未知记忆层: {layer}")

    async def _job_read(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        context = self.store.opportunity_context(self.opportunity_id)
        snapshots = context.get("job_snapshots") or []
        jobs = context.get("jobs") or []
        values: list[dict[str, Any]] = []
        refs: list[str] = []
        for snapshot in snapshots[:3]:
            payload = self._json_value(snapshot.get("payload"), {})
            values.append(
                {
                    "title": payload.get("title"),
                    "company": payload.get("company"),
                    "salary": payload.get("salary"),
                    "city": payload.get("city"),
                    "experience": payload.get("experience"),
                    "education": payload.get("education"),
                    "requirements": payload.get("requirements") or payload.get("description"),
                }
            )
            refs.append(f"boss_job_snapshot:{snapshot['id']}")
        if not values:
            for job in jobs[:3]:
                ref = f"boss_job_snapshot:{job['id']}"
                values.append(
                    {
                        "ref": ref,
                        **{
                            key: job.get(key)
                            for key in (
                                "title",
                                "company",
                                "salary",
                                "city",
                                "experience",
                                "education",
                            )
                        },
                    }
                )
                refs.append(ref)
        return {
            "ok": bool(values),
            "summary": f"读取到 {len(values)} 个岗位版本。",
            "facts": values,
            "evidence_refs": refs,
            "freshness": "local_current",
        }

    async def _profile_read(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        profile = self.store.opportunity_context(self.opportunity_id).get("candidate_profile") or {}
        summary = OpportunityBootstrapBuilder.redact(
            str(profile.get("profile_summary") or "")[:1600]
        )
        if not summary:
            return {
                "ok": False,
                "summary": "当前账号尚未生成候选人画像。",
                "facts": [],
                "evidence_refs": [],
                "freshness": "local_missing",
            }
        version = hashlib.sha256(
            json.dumps(
                {
                    "summary": summary,
                    "skills": profile.get("skill_tags") or [],
                    "projects": profile.get("project_tags") or [],
                    "agent": profile.get("agent_tags") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        ref = f"candidate_profile:{version}"
        return {
            "ok": True,
            "summary": "已读取脱敏候选人画像，可用于生成有依据的自我介绍。",
            "facts": [
                {
                    "ref": ref,
                    "summary": summary,
                    "skills": profile.get("skill_tags") or [],
                    "projects": profile.get("project_tags") or [],
                    "agent_capabilities": profile.get("agent_tags") or [],
                }
            ],
            "evidence_refs": [ref],
            "freshness": str(profile.get("updated_at") or "local_current"),
        }

    def _skill_handler(self, name: str) -> ToolHandler:
        async def load(_arguments: dict[str, Any]) -> dict[str, Any]:
            skill = self.skills.load(name, scope="opportunity")
            unlocked_tools = self._unlock_skill_tools(name)
            return {
                "ok": True,
                "summary": (
                    f"已加载领域 Skill：{skill['name']}。"
                    f"新增可用工具：{', '.join(unlocked_tools) or '无'}。"
                ),
                "facts": [{**skill, "available_tools": unlocked_tools}],
                "evidence_refs": [],
                "freshness": f"sha256:{skill['content_hash']}",
            }

        return load

    def _unlock_skill_tools(self, name: str) -> list[str]:
        """Reveal only evidence tools supported by the loaded Skill and local state."""

        grants = set(self.skills.tool_grants(name))
        context = self.store.opportunity_context(self.opportunity_id)
        unlocked: set[str] = set()

        if "memory_read" in grants:
            layers = set(self._memory_layers)
            hidden_messages = [
                message
                for message in context.get("messages") or []
                if bool(message.get("is_human_message", 1))
                and str(message.get("message_type") or "text")
                not in OpportunityBootstrapBuilder.IGNORED_TYPES
                and f"boss_message:{message.get('message_id')}" not in self._visible_evidence_refs
            ]
            if hidden_messages:
                layers.add("l1")
            if context.get("events"):
                layers.add("l2")
            self._memory_layers = frozenset(layers)
            if layers:
                unlocked.add("memory_read")

        if "job_read" in grants and (context.get("jobs") or context.get("job_snapshots")):
            self._job_read_enabled = True
            unlocked.add("job_read")

        if "profile_read" in grants and context.get("candidate_profile"):
            self._profile_read_enabled = True
            unlocked.add("profile_read")

        opportunity = context.get("opportunity") or {}
        for tool_name in grants & self.OPPORTUNITY_BOUND_MCP_TOOLS:
            tool = self._tools.get(tool_name)
            if (
                self._external_tools_allowed
                and tool is not None
                and tool.kind == "mcp"
                and not (tool_name == "research_company" and not opportunity.get("company"))
            ):
                self._enabled_mcp_tools = frozenset({*self._enabled_mcp_tools, tool_name})
                unlocked.add(tool_name)

        return sorted(unlocked)

    def _mcp_handler(self, name: str) -> ToolHandler:
        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            bound_arguments = dict(arguments)
            if name in self.OPPORTUNITY_BOUND_MCP_TOOLS:
                bound_arguments["opportunity_id"] = self.opportunity_id
            return await self.mcp_client.call(name, bound_arguments)

        return call

    @classmethod
    def _agent_visible_mcp_schema(
        cls,
        name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Hide capability-bound identifiers from model-controlled arguments."""

        visible = deepcopy(schema)
        if name not in cls.OPPORTUNITY_BOUND_MCP_TOOLS:
            return visible
        properties = dict(visible.get("properties") or {})
        properties.pop("opportunity_id", None)
        visible["properties"] = properties
        visible["required"] = [
            item for item in visible.get("required") or [] if item != "opportunity_id"
        ]
        return visible

    @staticmethod
    def _normalize_observation(value: dict[str, Any]) -> dict[str, Any]:
        failures = value.get("failures") or []
        error = value.get("error")
        if not error and failures and isinstance(failures[0], dict):
            error = failures[0].get("error")
        return {
            "ok": bool(value.get("ok", True)),
            "summary": str(value.get("summary") or value.get("error") or "工具已完成。"),
            "facts": value.get("facts") or value.get("messages") or value.get("sources") or [],
            "evidence_refs": sorted({str(ref) for ref in value.get("evidence_refs") or [] if ref}),
            "freshness": str(value.get("freshness") or "unknown"),
            **({"error": str(error)} if error else {}),
        }

    @staticmethod
    def _event_refs(events: list[dict[str, Any]]) -> list[str]:
        refs: set[str] = set()
        for event in events:
            values = ApplyToolbox._json_value(event.get("evidence_message_ids"), [])
            for value in values if isinstance(values, list) else []:
                ref = str(value)
                refs.add(ref if ":" in ref else f"boss_message:{ref}")
        return sorted(refs)

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if not isinstance(value, str):
            return value if value is not None else default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
