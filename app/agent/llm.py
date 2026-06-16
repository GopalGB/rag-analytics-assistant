"""LLM provider abstraction with native tool-calling. OpenAI-compatible via REST.

The engine treats the LLM as a protocol, so tests inject a fake and the app stays provider-agnostic.
`build_llm` returns None when no provider is configured — the engine then uses its deterministic fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class BaseLLM(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


class OpenAILLM:
    """Calls an OpenAI-compatible /chat/completions endpoint with tool-calling enabled."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        import requests  # imported lazily; only exercised on the OpenAI provider path

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "temperature": 0.1,
            },
            timeout=60,
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return LLMResponse(content=msg.get("content"), tool_calls=calls)


def build_llm(settings) -> BaseLLM | None:
    provider = settings.llm_provider
    if provider == "none":
        return None
    if provider in ("auto", "openai") and settings.openai_api_key:
        return OpenAILLM(settings.openai_api_key, settings.openai_base_url, settings.openai_model)
    return None
