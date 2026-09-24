"""LLM provider abstraction. Four backends, one interface.

Each provider implements:
- `converse(...)`: given a system prompt, prior turn history, the new question, and a ToolBox, run the
  full tool-calling conversation in its OWN native format and return the final answer text. Tool side
  effects (SQL run, sources, proposed actions) are recorded on the ToolBox, so the engine stays
  provider-agnostic.
- `complete(system, prompt)`: a single tool-free completion (used for invoice field extraction).

- OllamaLLM   — a LOCAL model served by Ollama on this machine (recommended; nothing leaves the Mac).
- OpenAILLM   — OpenAI-compatible /chat/completions with native tool-calling (cloud, or a local server).
- BedrockLLM  — AWS Bedrock Runtime `converse` with native tool use (cloud; boto3, lazy import).
- CommandLLM  — any CLI: prompt in on stdin, completion out on stdout, text-based tool protocol.

Privacy gate: a provider that sends data off the machine is only built when ALLOW_CLOUD_AI=true.
`select_llm` returns the provider (or None) plus a human-readable note explaining the choice.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Protocol
from urllib.parse import urlparse

from app.agent.tools import TOOLS, ToolBox


class BaseLLM(Protocol):
    supports_tools: bool
    name: str
    is_local: bool

    def converse(
        self, system: str, history: list[dict[str, str]], question: str, toolbox: ToolBox, max_iters: int
    ) -> str: ...

    def complete(self, system: str, prompt: str) -> str: ...


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


# --------------------------------------------------------------------------- OpenAI
class OpenAILLM:
    supports_tools = True

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 120):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.is_local = _is_local_url(self.base_url)
        self.name = f"openai-compatible:{model}"

    def _call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        import requests  # imported lazily; only exercised on the OpenAI provider path

        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=self.timeout,
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

    def complete(self, system: str, prompt: str) -> str:
        msg = self._call([{"role": "system", "content": system}, {"role": "user", "content": prompt}], None)
        return msg.get("content") or ""


# --------------------------------------------------------------------------- Ollama (local)
class OllamaLLM(OpenAILLM):
    """A model running locally under Ollama (https://ollama.com), via its OpenAI-compatible API.

    On Apple Silicon this runs on the GPU through Metal. No API key; no data leaves the machine."""

    def __init__(self, base_url: str, model: str, timeout: int = 300):
        super().__init__(api_key="ollama", base_url=base_url, model=model, timeout=timeout)
        self.name = f"ollama:{model}"

    @staticmethod
    def reachable(base_url: str, timeout: float = 0.8) -> bool:
        import requests

        root = base_url.rstrip("/").removesuffix("/v1")
        try:
            return requests.get(f"{root}/api/tags", timeout=timeout).ok
        except Exception:
            return False


# --------------------------------------------------------------------------- Bedrock
class BedrockLLM:
    supports_tools = True

    is_local = False

    def __init__(self, model_id: str, region: str, client: Any | None = None):
        self.model_id = model_id
        self.name = f"bedrock:{model_id}"
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

    def complete(self, system: str, prompt: str) -> str:
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.0},
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

    def __init__(self, command: str, timeout: int = 120, is_local: bool = False):
        self.command = command
        self.timeout = timeout
        self.is_local = is_local  # set by the operator: a CLI may call a cloud service
        self.name = "cli:" + command.split()[0] if command.split() else "cli"

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

    def complete(self, system: str, prompt: str) -> str:
        return self._run(self._render(system, [], prompt))


# --------------------------------------------------------------------------- selection
_CLOUD_BLOCKED = (
    "{what} would send questions and document extracts off this machine, which needs explicit approval. "
    "Set ALLOW_CLOUD_AI=true once approved, or use a local model (Ollama)."
)


def select_llm(settings) -> tuple[BaseLLM | None, str]:
    """Pick a provider. Returns (llm or None, note explaining the choice for the status/privacy page)."""
    provider = settings.llm_provider
    allow_cloud = settings.allow_cloud_ai

    def ollama(probe: bool):
        if not settings.ollama_model:
            return None, "Ollama model not set (OLLAMA_MODEL)."
        if probe and not OllamaLLM.reachable(settings.ollama_base_url):
            return None, f"Ollama is not running at {settings.ollama_base_url}."
        return OllamaLLM(settings.ollama_base_url, settings.ollama_model, settings.llm_timeout), "Local model via Ollama."

    def openai():
        if not settings.openai_api_key:
            return None, "OPENAI_API_KEY not set."
        llm = OpenAILLM(settings.openai_api_key, settings.openai_base_url, settings.openai_model, settings.llm_timeout)
        if not llm.is_local and not allow_cloud:
            return None, _CLOUD_BLOCKED.format(what=f"The cloud model at {settings.openai_base_url}")
        return llm, "Local OpenAI-compatible server." if llm.is_local else "Cloud model (approved via ALLOW_CLOUD_AI)."

    def bedrock():
        if not settings.bedrock_model_id:
            return None, "BEDROCK_MODEL_ID not set."
        if not allow_cloud:
            return None, _CLOUD_BLOCKED.format(what="AWS Bedrock")
        return BedrockLLM(settings.bedrock_model_id, settings.aws_region), "Cloud model on AWS Bedrock (approved)."

    def cli():
        if not settings.llm_cli_command:
            return None, "LLM_CLI_COMMAND not set."
        if not settings.llm_cli_is_local and not allow_cloud:
            return None, _CLOUD_BLOCKED.format(what="The configured CLI model")
        llm = CommandLLM(settings.llm_cli_command, settings.llm_cli_timeout, is_local=settings.llm_cli_is_local)
        return llm, "Local CLI model." if llm.is_local else "CLI model (approved via ALLOW_CLOUD_AI)."

    if provider == "none":
        return None, "AI model disabled (LLM_PROVIDER=none)."
    if provider == "ollama":
        return ollama(probe=False)
    if provider == "openai":
        return openai()
    if provider == "bedrock":
        return bedrock()
    if provider == "cli":
        return cli()
    # auto: prefer a local model, then an approved cloud API, then a CLI.
    notes = []
    for pick in (lambda: ollama(probe=True), openai, bedrock, cli):
        llm, note = pick()
        if llm is not None:
            return llm, note
        notes.append(note)
    blocked = [n for n in notes if "explicit approval" in n]
    return None, blocked[0] if blocked else "No AI model configured. Install Ollama for a local model (see docs)."


def build_llm(settings) -> BaseLLM | None:
    return select_llm(settings)[0]
