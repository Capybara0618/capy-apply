"""Read-only BOSS evidence MCP server."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from capybot.apply.boss_reader import BossJobDetailReader, BossOpportunityReader

mcp = FastMCP(
    "capybot-boss-readonly",
    instructions="Only refresh BOSS evidence. This server has no write or send tools.",
    log_level="ERROR",
)


@mcp.tool(
    name="boss_refresh_opportunity",
    title="刷新 BOSS 机会证据",
    description=(
        "重新读取指定机会关联的 BOSS 消息和岗位卡，并返回新增消息与版本化证据引用。"
        "仅在本地证据缺失、过期或冲突时调用。"
    ),
    structured_output=True,
)
async def boss_refresh_opportunity(
    opportunity_id: str,
    max_pages: Annotated[int, Field(ge=1, le=5)] = 3,
) -> dict[str, Any]:
    pages = min(5, max(1, int(max_pages)))
    return await BossOpportunityReader().read(opportunity_id, max_pages=pages)


@mcp.tool(
    name="boss_fetch_job_detail",
    title="读取 BOSS 岗位详情",
    description=(
        "根据当前机会已保存的 BOSS 岗位 ID，读取职责、要求、薪资、地点、"
        "经验、学历和实习条件，并保存版本化岗位证据。"
        "工具不接受自由岗位 ID，不搜索其他岗位，也不执行投递或沟通。"
    ),
    structured_output=True,
)
async def boss_fetch_job_detail(opportunity_id: str) -> dict[str, Any]:
    return await BossJobDetailReader().read(opportunity_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
