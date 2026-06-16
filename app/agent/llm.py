"""LLM provider abstraction. Three backends, one interface.

Each provider implements `converse(...)`: given a system prompt, prior turn history, the new
question, and a ToolBox, it runs the full tool-calling conversation in its OWN native format and
returns the final answer text. Tool side effects (SQL run, sources) are recorded on the ToolBox, so
the engine stays provider-agnostic.

- OpenAILLM   — OpenAI-compatible /chat/completions with native tool-calling.
- BedrockLLM  — AWS Bedrock Runtime `converse` with native tool use (boto3, lazy import).
- CommandLLM  — any local CLI (e.g. the ChatGPT/Codex CLI): prompt in on stdin, completion out on
                stdout, with a text-based tool protocol for models without native tool-calling.

`build_llm` picks a provider from settings; returns None only when a provider is explicitly disabled
or unconfigured. The app is LLM-first: with `require_llm` set (the default) startup fails fast rather
than running without a model.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Protocol

from app.agent.tools import TOOLS, ToolBox


class BaseLLM(Protocol):
    supports_tools: bool

    def converse(
        self, system: str, history: list[dict[str, str]], question: str, toolbox: ToolBox, max_iters: int
    ) -> str: ...


# --------------------------------------------------------------------------- OpenAI
class OpenAILLM:
    supports_tools = True

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        import requests  # imported lazily; only exercised on the OpenAI provider path

        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]

    def converse(self, system, history, question, toolbox, max_iters):
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": question})

        for _ in range(max_iters):
            msg = self._call(messages, TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or ""
            messages.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
            )
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = toolbox.run(fn.get("name", ""), args)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(result)[:8000]}
                )

        final = self._call(messages + [{"role": "user", "content": "Give your best final answer now."}], None)
        return final.get("content") or ""


# --------------------------------------------------------------------------- Bedrock
class BedrockLLM:
    supports_tools = True

    def __init__(self, model_id: str, region: str, client: Any | None = None):
        self.model_id = model_id
        if client is None:
            import boto3  # imported lazily; only exercised on the Bedrock provider path

            client = boto3.client("bedrock-runtime", region_name=region)
        self.client = client

    @staticmethod
    def _tool_config() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "inputSchema": {"json": t["function"]["parameters"]},
                    }
                }
                for t in TOOLS
            ]
        }

    @staticmethod
    def _text(message: dict[str, Any]) -> str:
        return " ".join(c["text"] for c in message.get("content", []) if "text" in c).strip()

    def converse(self, system, history, question, toolbox, max_iters):
        messages: list[dict[str, Any]] = [
            {"role": m["role"], "content": [{"text": m["content"]}]} for m in history
        ]
        messages.append({"role": "user", "content": [{"text": question}]})
        system_blocks = [{"text": system}]
        tool_config = self._tool_config()

        for _ in range(max_iters):
            resp = self.client.converse(
                modelId=self.model_id,
                system=system_blocks,
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={"temperature": 0.1},
            )
            out = resp["output"]["message"]
            messages.append(out)
            tool_uses = [c["toolUse"] for c in out.get("content", []) if "toolUse" in c]
            if not tool_uses:
                return self._text(out)
            results = []
            for tu in tool_uses:
                result = toolbox.run(tu["name"], tu.get("input", {}) or {})
                results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": result}]}})
            messages.append({"role": "user", "content": results})

        messages.append({"role": "user", "content": [{"text": "Give your best final answer now."}]})
        resp = self.client.converse(
            modelId=self.model_id,
            system=system_blocks,
            messages=messages,
            inferenceConfig={"temperature": 0.1},
        )
        return self._text(resp["output"]["message"])


# --------------------------------------------------------------------------- CLI / command
_TOOL_LINE = re.compile(r"\s*TOOL\s+(\w+)\s+(\{.*\})\s*$", re.S)


def _tool_protocol_instructions() -> str:
    lines = [
        "You can use tools. To call one, reply with EXACTLY one line and nothing else:",
        "TOOL <tool_name> <json-arguments>",
        "Available tools:",
    ]
    for t in TOOLS:
        fn = t["function"]
        lines.append(f"- {fn['name']}: {fn['description']}")
    lines.append("After each TOOL_RESULT, call another tool or reply with your final plain-text answer.")
    return "\n".join(lines)


class CommandLLM:
    """Runs a configured local command per turn: full prompt on stdin, completion on stdout."""

    supports_tools = False

    def __init__(self, command: str, timeout: int = 120):
        self.command = command
        self.timeout = timeout

    def _run(self, prompt: str) -> str:
        try:
            proc = subprocess.run(
                self.command, shell=True, input=prompt, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    @staticmethod
    def _render(system: str, turns: list[dict[str, str]], question: str) -> str:
        parts = [f"[SYSTEM]\n{system}"]
        for t in turns:
            parts.append(f"[{t['role'].upper()}]\n{t['content']}")
        parts.append(f"[USER]\n{question}")
        parts.append("[ASSISTANT]")
        return "\n\n".join(parts)

    def converse(self, system, history, question, toolbox, max_iters):
        system = system + "\n\n" + _tool_protocol_instructions()
        turns = list(history)
        for _ in range(max_iters):
            out = self._run(self._render(system, turns, question))
            m = _TOOL_LINE.match(out)
            if not m:
                return out
            name = m.group(1)
            try:
                args = json.loads(m.group(2))
            except json.JSONDecodeError:
                args = {}
            result = toolbox.run(name, args)
            turns.append({"role": "assistant", "content": out})
            turns.append({"role": "user", "content": f"TOOL_RESULT[{name}]: {json.dumps(result)[:4000]}"})
        return self._run(self._render(system + "\nGive your final answer now.", turns, question))


# --------------------------------------------------------------------------- selection
def build_llm(settings) -> BaseLLM | None:
    provider = settings.llm_provider

    def openai_if_possible():
        if settings.openai_api_key:
            return OpenAILLM(settings.openai_api_key, settings.openai_base_url, settings.openai_model)
        return None

    def bedrock_if_possible():
        if settings.bedrock_model_id:
            return BedrockLLM(settings.bedrock_model_id, settings.aws_region)
        return None

    def cli_if_possible():
        if settings.llm_cli_command:
            return CommandLLM(settings.llm_cli_command, settings.llm_cli_timeout)
        return None

    if provider == "none":
        return None
    if provider == "openai":
        return openai_if_possible()
    if provider == "bedrock":
        return bedrock_if_possible()
    if provider == "cli":
        return cli_if_possible()
    # auto: prefer a configured cloud API, then a local CLI. None means "nothing configured" —
    # with require_llm set (the default) the app refuses to start rather than run without a model.
    return openai_if_possible() or bedrock_if_possible() or cli_if_possible()
