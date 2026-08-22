"""Client for real MCP subprocesses used by the opportunity Harness."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    command: str
    args: tuple[str, ...]
    allowed_tools: frozenset[str]
    tools: tuple[MCPTool, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


class MCPToolClient:
    """Expose declared schemas and lazily open allowlisted MCP sessions on first use."""

    def __init__(self, specs: list[MCPServerSpec], *, timeout_s: float = 45) -> None:
        self.specs = specs
        self.timeout_s = timeout_s
        self._stack: AsyncExitStack | None = None
        self._specs_by_name = {spec.name: spec for spec in specs}
        self._sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, MCPTool] = {}

    async def __aenter__(self) -> "MCPToolClient":
        self._stack = AsyncExitStack()
        for spec in self.specs:
            if spec.tools:
                for tool in spec.tools:
                    if tool.server != spec.name or tool.name not in spec.allowed_tools:
                        raise ValueError(f"invalid declared MCP tool: {tool.name}")
                    self._register_tool(tool)
            else:
                await self._connect(spec, register_discovered=True)
        return self

    def _register_tool(self, tool: MCPTool) -> None:
        if tool.name in self._tools:
            raise RuntimeError(f"MCP tool name collision: {tool.name}")
        self._tools[tool.name] = tool

    async def _connect(
        self,
        spec: MCPServerSpec,
        *,
        register_discovered: bool,
    ) -> ClientSession:
        if spec.name in self._sessions:
            return self._sessions[spec.name]
        if self._stack is None:
            raise RuntimeError("MCPToolClient is not active")
        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            env={**os.environ, **spec.env},
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self.timeout_s),
            )
        )
        await session.initialize()
        listed = await session.list_tools()
        advertised = {tool.name: tool for tool in listed.tools}
        missing = spec.allowed_tools - advertised.keys()
        if missing:
            raise RuntimeError(
                f"MCP server {spec.name} did not advertise: {', '.join(sorted(missing))}"
            )
        self._validate_declared_contracts(spec, advertised)
        self._sessions[spec.name] = session
        if register_discovered:
            for name, tool in advertised.items():
                if name not in spec.allowed_tools:
                    continue
                self._register_tool(
                    MCPTool(
                        name=name,
                        description=tool.description or "",
                        input_schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
                        server=spec.name,
                    )
                )
        return session

    @classmethod
    def _validate_declared_contracts(
        cls,
        spec: MCPServerSpec,
        advertised: dict[str, Any],
    ) -> None:
        """Fail before execution when a lazy client schema drifted from its server."""

        for declared in spec.tools:
            remote = advertised.get(declared.name)
            if remote is None:
                continue
            expected = cls._contract_signature(declared.input_schema)
            actual = cls._contract_signature(dict(remote.inputSchema or {}))
            if expected != actual:
                raise RuntimeError(
                    f"MCP schema drift for {spec.name}.{declared.name}: "
                    f"declared={expected!r}, advertised={actual!r}"
                )

    @staticmethod
    def _contract_signature(schema: dict[str, Any]) -> dict[str, Any]:
        relevant_keys = ("type", "enum", "minimum", "maximum")
        properties = {
            str(name): {
                key: value
                for key in relevant_keys
                if (value := dict(detail or {}).get(key)) is not None
            }
            for name, detail in dict(schema.get("properties") or {}).items()
        }
        return {
            "properties": properties,
            "required": sorted(str(item) for item in schema.get("required") or []),
        }

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._sessions.clear()
        self._tools.clear()

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def server_for(self, name: str) -> str | None:
        tool = self._tools.get(name)
        return tool.server if tool else None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise PermissionError(f"工具不在 Apply MCP 白名单中: {name}")
        spec = self._specs_by_name[tool.server]
        session = await self._connect(spec, register_discovered=False)
        result = await session.call_tool(name, arguments)
        if result.isError:
            detail = "；".join(
                str(getattr(item, "text", "MCP tool error")) for item in result.content
            )
            raise RuntimeError(detail or f"MCP tool failed: {name}")
        if result.structuredContent is not None:
            return dict(result.structuredContent)
        texts = [
            str(getattr(item, "text", ""))
            for item in result.content
            if getattr(item, "type", "") == "text"
        ]
        if not texts:
            return {}
        joined = "\n".join(texts)
        try:
            value = json.loads(joined)
        except json.JSONDecodeError:
            return {"text": joined}
        return value if isinstance(value, dict) else {"result": value}
