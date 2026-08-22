"""Planner model boundary without dependencies on Capybot's former chat Agent."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI

from .schema import decision_json_schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_mode: str = "unknown"


class PlannerModel(Protocol):
    provider_label: str
    model_label: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn: ...


class OpenAIPlannerModel:
    """OpenAI-compatible chat-completions adapter with native tool calling."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float = 90,
    ) -> None:
        settings = self._local_settings()
        self.api_key = (
            api_key
            or os.getenv("CAPYBOT_APPLY_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or settings.get("openai_api_key")
            or settings.get("api_key")
        )
        self.base_url = (
            base_url
            or os.getenv("CAPYBOT_APPLY_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or settings.get("openai_base_url")
            or settings.get("base_url")
        )
        self.model = (
            model
            or os.getenv("CAPYBOT_APPLY_MODEL")
            or os.getenv("OPENAI_MODEL")
            or settings.get("model")
            or "gpt-4o-mini"
        )
        self.timeout_s = timeout_s
        self.provider_label = "openai-compatible"
        self.model_label = self.model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _local_settings() -> dict[str, Any]:
        path = Path.home() / ".capybot" / "apply" / "local_settings.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        if not self.available:
            raise RuntimeError("未配置 OpenAI-compatible API key")
        return await asyncio.to_thread(
            self._complete_sync,
            messages,
            tools,
            decision_json_schema(),
            "capybot_opportunity_decision",
        )

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any],
        schema_name: str,
    ) -> ModelTurn:
        if not self.available:
            raise RuntimeError("未配置 OpenAI-compatible API key")
        return await asyncio.to_thread(
            self._complete_sync,
            messages,
            [],
            schema,
            schema_name,
        )

    def _complete_sync(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response_schema: dict[str, Any],
        schema_name: str,
    ) -> ModelTurn:
        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout_s)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": response_schema,
                },
            },
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        response_mode = "json_schema"
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            message = str(exc).lower()
            if "response_format" not in message and "json_schema" not in message:
                raise
            kwargs.pop("response_format", None)
            response_mode = "json_fallback"
            response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"工具 {call.function.name} 的参数不是合法 JSON") from exc
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                )
            )
        usage = response.usage
        return ModelTurn(
            content=message.content,
            tool_calls=calls,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            response_mode=response_mode,
        )
