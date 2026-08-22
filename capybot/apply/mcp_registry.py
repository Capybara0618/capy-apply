"""Production MCP server registry and shared client-side contracts."""

from __future__ import annotations

import sys

from .agent_runtime.mcp_client import MCPServerSpec, MCPTool, MCPToolClient

BOSS_REFRESH_CONTRACT = MCPTool(
    name="boss_refresh_opportunity",
    description=(
        "补查当前机会的最新 BOSS 消息和岗位快照。"
        "当前机会由 Runtime 绑定，仅在本地证据缺失、过期或冲突时调用。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "opportunity_id": {"type": "string"},
            "max_pages": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["opportunity_id"],
        "additionalProperties": False,
    },
    server="boss",
)

BOSS_JOB_DETAIL_CONTRACT = MCPTool(
    name="boss_fetch_job_detail",
    description=(
        "从 BOSS 读取当前机会对应岗位的完整详情，并保存版本化岗位证据。"
        "当前机会由 Runtime 绑定，只在本地岗位职责或条件不完整时调用。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "opportunity_id": {"type": "string"},
        },
        "required": ["opportunity_id"],
        "additionalProperties": False,
    },
    server="boss",
)

COMPANY_RESEARCH_CONTRACT = MCPTool(
    name="research_company",
    description=(
        "核验当前机会的公司背景。当前机会由 Runtime 绑定，"
        "只能选择基础信息、业务或招聘情况，不能提交自由搜索词。"
    ),
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


def create_apply_mcp_client(env: dict[str, str] | None = None) -> MCPToolClient:
    """Create the two allowlisted, lazily connected Apply MCP servers."""

    server_env = dict(env or {})
    return MCPToolClient(
        [
            MCPServerSpec(
                name="boss",
                command=sys.executable,
                args=("-m", "capybot.mcp_servers.boss"),
                allowed_tools=frozenset(
                    {"boss_refresh_opportunity", "boss_fetch_job_detail"}
                ),
                tools=(BOSS_REFRESH_CONTRACT, BOSS_JOB_DETAIL_CONTRACT),
                env=server_env,
            ),
            MCPServerSpec(
                name="company-intel",
                command=sys.executable,
                args=("-m", "capybot.mcp_servers.intel"),
                allowed_tools=frozenset({"research_company"}),
                tools=(COMPANY_RESEARCH_CONTRACT,),
                env=server_env,
            ),
        ]
    )

