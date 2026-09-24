"""LLM providers. One interface, many backends.

Every provider implements:
- `converse(system, history, question, toolbox, max_iters) -> str`: run the full tool-calling
  conversation in the provider's own native format. The tool list comes from the ToolBox (so the task
  router can restrict tools per request) and every call is executed — and validated — by the ToolBox.
  With `stream=sink`, providers that support it stream the answer: `sink("text", delta)` for each
  chunk and `sink("reset", None)` when a turn turns out to be a tool-calling turn (its preamble is
  discarded). Providers without streaming just emit the full text once at the end.
- `complete(system, prompt) -> str`: one tool-free completion.
- `complete_json(system, prompt, schema, name) -> str`: a completion constrained to a JSON Schema using
  the provider's strongest mechanism (OpenAI `json_schema`, JSON mode, or Anthropic tool forcing).

Attributes: `name` ("provider:model"), `provider`, `model`, `is_local` (True only when the model runs
on this machine or the local network). Token usage is reported to `app.llm.usage`.

Backends:
- OpenAICompatLLM — OpenAI and every OpenAI-compatible API: OpenRouter, Groq, Mistral, DeepSeek,
                    Together, xAI, Google Gemini (OpenAI endpoint), LM Studio / vLLM on localhost.
- AzureOpenAILLM  — Azure OpenAI deployments.
- AnthropicLLM    — Claude via the native Messages API (tool use).
- OllamaLLM       — local models via Ollama.
- BedrockLLM      — AWS Bedrock `converse`.
- CommandLLM      — any CLI (prompt on stdin, answer on stdout) with a text tool protocol.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from typing import Any, Protocol
from urllib.parse import urlparse

from app.llm import usage


class LLMError(RuntimeError):
    """A provider call failed. `retryable` = worth trying the next model."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class BaseLLM(Protocol):
    name: str
    provider: str
    model: str
    is_local: bool
    supports_tools: bool

    def converse(self, system: str, history: list[dict[str, str]], question: str, toolbox: Any, max_iters: int) -> str: ...

    def complete(self, system: str, prompt: str) -> str: ...


def is_local_url(url: str) -> bool:
    """True for this machine or a private/LAN address (an on-premises model server)."""
    host = (urlparse(url).hostname or "").lower()
    # host.docker.internal = the Mac itself when the app runs in a container (e.g. Ollama on the host)
    if host in {"localhost", "0.0.0.0", "host.docker.internal"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _sse_data(resp: Any):
    """Yield parsed JSON payloads from a server-sent-events response (`data: {...}` lines)."""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _emit_once(sink: Any, text: str) -> str:
    if sink is not None and text:
        sink("text", text)
    return text


def _tool_specs(toolbox: Any) -> list[dict[str, Any]]:
    return toolbox.tool_specs() if hasattr(toolbox, "tool_specs") else []


def _http_error(resp: Any, provider: str) -> LLMError:
    status = getattr(resp, "status_code", None)
    try:
        detail = json.dumps(resp.json())[:300]
    except Exception:
        detail = (getattr(resp, "text", "") or "")[:300]
    kind = {401: "authentication failed (check the API key)", 403: "permission denied", 404: "model or endpoint not found",
            429: "rate limited / quota exceeded"}.get(status, "request failed")
    return LLMError(f"{provider}: {kind} (HTTP {status}) {detail}", status=status)


# --------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatLLM:
    supports_tools = True

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        model: str,
        provider: str = "openai",
        timeout: int = 120,
        json_mode: str = "json_object",  # json_schema | json_object | none
        extra_headers: dict[str, str] | None = None,
        http: Any = None,
        is_local: bool | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.json_mode = json_mode
        self.extra_headers = extra_headers or {}
        self.is_local = is_local_url(self.base_url) if is_local is None else is_local
        self.name = f"{provider}:{model}"
        self._http = http

    @property
    def http(self):
        if self._http is None:
            import requests

            self._http = requests
        return self._http

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1, **extra}
        if tools:
            body["tools"] = tools
        resp = self.http.post(self._url(), headers=self._headers(), json=body, timeout=self.timeout)
        if resp.status_code >= 400:
            raise _http_error(resp, self.provider)
        data = resp.json()
        u = data.get("usage") or {}
        usage.add(u.get("prompt_tokens"), u.get("completion_tokens"))
        return data["choices"][0]["message"]

    def _call_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, sink: Any) -> dict[str, Any]:
        """Streaming chat call; returns the assembled message ({content, tool_calls}) like `_call`."""
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.1, "stream": True,
                                "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = tools
        resp = self.http.post(self._url(), headers=self._headers(), json=body, timeout=self.timeout, stream=True)
        if resp.status_code == 400:  # some servers reject stream_options
            body.pop("stream_options")
            resp = self.http.post(self._url(), headers=self._headers(), json=body, timeout=self.timeout, stream=True)
        if resp.status_code >= 400:
            raise _http_error(resp, self.provider)
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        for chunk in _sse_data(resp):
            u = chunk.get("usage") or {}
            if u:
                usage.add(u.get("prompt_tokens"), u.get("completion_tokens"))
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    if not calls:
                        sink("text", delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "type": "function",
                                                                            "function": {"name": "", "arguments": ""}})
                    if not slot["id"] and tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
        if calls and content:
            sink("reset", None)  # the streamed preamble belonged to a tool-calling turn
        return {"content": "".join(content), "tool_calls": [calls[i] for i in sorted(calls)]}

    def converse(self, system, history, question, toolbox, max_iters, stream=None):
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history,
                                          {"role": "user", "content": question}]
        tools = _tool_specs(toolbox)
        call = (lambda m, t: self._call_stream(m, t, stream)) if stream else self._call
        for _ in range(max_iters):
            msg = call(messages, tools)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content") or ""
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": fn.get("arguments")}
                result = toolbox.run(fn.get("name", ""), args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(result, default=str)[:8000]})
        final = call(messages + [{"role": "user", "content": "Give your best final answer now."}], None)
        return final.get("content") or ""

    def complete(self, system: str, prompt: str) -> str:
        return self._call([{"role": "system", "content": system}, {"role": "user", "content": prompt}]).get("content") or ""

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], name: str = "result") -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if self.json_mode == "json_schema":
            rf = {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": False}}
        elif self.json_mode == "json_object":
            rf = {"type": "json_object"}
        else:
            return self._call(messages).get("content") or ""
        try:
            return self._call(messages, response_format=rf).get("content") or ""
        except LLMError as exc:
            if exc.status == 400:  # model/route doesn't support response_format: plain call, still validated
                return self._call(messages).get("content") or ""
            raise


class OpenAILLM(OpenAICompatLLM):
    """OpenAI (or any OpenAI-compatible endpoint given by base_url)."""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 120, http: Any = None):
        official = "api.openai.com" in base_url
        super().__init__(api_key, base_url, model, provider="openai", timeout=timeout,
                         json_mode="json_schema" if official else "json_object", http=http)


class OllamaLLM(OpenAICompatLLM):
    """A model served by Ollama (https://ollama.com) on this machine. No key; nothing leaves the Mac."""

    def __init__(self, base_url: str, model: str, timeout: int = 300, http: Any = None):
        super().__init__(None, base_url, model, provider="ollama", timeout=timeout, json_mode="json_object", http=http)

    @staticmethod
    def reachable(base_url: str, timeout: float = 0.8) -> bool:
        import requests

        root = base_url.rstrip("/").removesuffix("/v1")
        try:
            return requests.get(f"{root}/api/tags", timeout=timeout).ok
        except Exception:
            return False


class AzureOpenAILLM(OpenAICompatLLM):
    """Azure OpenAI: `model` is the deployment name."""

    def __init__(self, api_key: str, endpoint: str, deployment: str, api_version: str, timeout: int = 120, http: Any = None):
        super().__init__(api_key, endpoint, deployment, provider="azure", timeout=timeout, json_mode="json_schema", http=http)
        self.api_version = api_version

    def _url(self) -> str:
        return f"{self.base_url}/openai/deployments/{self.model}/chat/completions?api-version={self.api_version}"

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "api-key": self.api_key or ""}


# --------------------------------------------------------------------------- Anthropic (native)
class AnthropicLLM:
    """Claude via the Anthropic Messages API with native tool use."""

    supports_tools = True
    is_local = False
    provider = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com", timeout: int = 120,
                 max_tokens: int = 2048, http: Any = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.name = f"anthropic:{model}"
        self._http = http

    @property
    def http(self):
        if self._http is None:
            import requests

            self._http = requests
        return self._http

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = self.http.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": self.API_VERSION, "content-type": "application/json"},
            json={"model": self.model, "max_tokens": self.max_tokens, **body},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise _http_error(resp, self.provider)
        data = resp.json()
        u = data.get("usage") or {}
        usage.add(u.get("input_tokens"), u.get("output_tokens"))
        return data

    @staticmethod
    def _tools(toolbox: Any) -> list[dict[str, Any]]:
        return [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "input_schema": t["function"]["parameters"]}
            for t in _tool_specs(toolbox)
        ]

    @staticmethod
    def _text(content: list[dict[str, Any]]) -> str:
        return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()

    def _post_stream(self, body: dict[str, Any], sink: Any) -> dict[str, Any]:
        """Streaming Messages call; returns {"content": [...blocks]} like `_post`."""
        resp = self.http.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": self.API_VERSION, "content-type": "application/json"},
            json={"model": self.model, "max_tokens": self.max_tokens, "stream": True, **body},
            timeout=self.timeout,
            stream=True,
        )
        if resp.status_code >= 400:
            raise _http_error(resp, self.provider)
        blocks: dict[int, dict[str, Any]] = {}
        partial: dict[int, list[str]] = {}
        saw_tool = False
        for ev in _sse_data(resp):
            kind = ev.get("type")
            if kind == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                usage.add(u.get("input_tokens"), u.get("output_tokens"))
            elif kind == "content_block_start":
                block = dict(ev.get("content_block") or {})
                if block.get("type") == "tool_use":
                    saw_tool = True
                    block["input"] = {}
                blocks[ev.get("index", len(blocks))] = block
            elif kind == "content_block_delta":
                i, d = ev.get("index", 0), ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    blocks.setdefault(i, {"type": "text", "text": ""})
                    blocks[i]["text"] = blocks[i].get("text", "") + d.get("text", "")
                    if not saw_tool:
                        sink("text", d.get("text", ""))
                elif d.get("type") == "input_json_delta":
                    partial.setdefault(i, []).append(d.get("partial_json", ""))
            elif kind == "message_delta":
                u = ev.get("usage") or {}
                usage.add(None, u.get("output_tokens"))
            elif kind == "error":
                raise LLMError(f"anthropic: stream error {ev.get('error')}")
        for i, parts in partial.items():
            try:
                blocks[i]["input"] = json.loads("".join(parts) or "{}")
            except json.JSONDecodeError:
                blocks[i]["input"] = {"__invalid_json__": "".join(parts)}
        if saw_tool and any(b.get("type") == "text" and b.get("text") for b in blocks.values()):
            sink("reset", None)
        return {"content": [blocks[i] for i in sorted(blocks)]}

    def converse(self, system, history, question, toolbox, max_iters, stream=None):
        messages: list[dict[str, Any]] = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": question})
        tools = self._tools(toolbox)
        post = (lambda b: self._post_stream(b, stream)) if stream else self._post
        for _ in range(max_iters):
            data = post({"system": system, "messages": messages, **({"tools": tools} if tools else {})})
            content = data.get("content", [])
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                return self._text(content)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"],
                 "content": json.dumps(toolbox.run(b["name"], b.get("input") or {}), default=str)[:8000]}
                for b in uses
            ]})
        messages.append({"role": "user", "content": "Give your best final answer now, without calling tools."})
        return self._text(post({"system": system, "messages": messages}).get("content", []))

    def complete(self, system: str, prompt: str) -> str:
        return self._text(self._post({"system": system, "messages": [{"role": "user", "content": prompt}]}).get("content", []))

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], name: str = "result") -> str:
        """Tool forcing: the model must 'call' a tool whose input schema is the target schema."""
        tool = {"name": "emit_" + re.sub(r"\W", "_", name.lower())[:40], "description": "Return the result.",
                "input_schema": schema}
        data = self._post({
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
        })
        for b in data.get("content", []):
            if b.get("type") == "tool_use":
                return json.dumps(b.get("input") or {})
        return self._text(data.get("content", []))


# --------------------------------------------------------------------------- Bedrock
class BedrockLLM:
    supports_tools = True
    is_local = False
    provider = "bedrock"

    def __init__(self, model_id: str, region: str, client: Any | None = None):
        self.model_id = self.model = model_id
        self.name = f"bedrock:{model_id}"
        if client is None:
            import boto3  # lazy: only on the Bedrock path

            client = boto3.client("bedrock-runtime", region_name=region)
        self.client = client

    @staticmethod
    def _tool_config(toolbox: Any) -> dict[str, Any]:
        return {"tools": [
            {"toolSpec": {"name": t["function"]["name"], "description": t["function"]["description"],
                          "inputSchema": {"json": t["function"]["parameters"]}}}
            for t in _tool_specs(toolbox)
        ]}

    @staticmethod
    def _text(message: dict[str, Any]) -> str:
        return " ".join(c["text"] for c in message.get("content", []) if "text" in c).strip()

    def _converse(self, **kw: Any) -> dict[str, Any]:
        resp = self.client.converse(modelId=self.model_id, **kw)
        u = resp.get("usage") or {}
        usage.add(u.get("inputTokens"), u.get("outputTokens"))
        return resp

    def converse(self, system, history, question, toolbox, max_iters, stream=None):
        return _emit_once(stream, self._converse_all(system, history, question, toolbox, max_iters))

    def _converse_all(self, system, history, question, toolbox, max_iters):
        messages: list[dict[str, Any]] = [{"role": m["role"], "content": [{"text": m["content"]}]} for m in history]
        messages.append({"role": "user", "content": [{"text": question}]})
        system_blocks = [{"text": system}]
        tool_config = self._tool_config(toolbox)
        for _ in range(max_iters):
            kw: dict[str, Any] = {"system": system_blocks, "messages": messages, "inferenceConfig": {"temperature": 0.1}}
            if tool_config["tools"]:
                kw["toolConfig"] = tool_config
            out = self._converse(**kw)["output"]["message"]
            messages.append(out)
            tool_uses = [c["toolUse"] for c in out.get("content", []) if "toolUse" in c]
            if not tool_uses:
                return self._text(out)
            messages.append({"role": "user", "content": [
                {"toolResult": {"toolUseId": tu["toolUseId"],
                                "content": [{"json": json.loads(json.dumps(toolbox.run(tu["name"], tu.get("input", {}) or {}), default=str))}]}}
                for tu in tool_uses
            ]})
        messages.append({"role": "user", "content": [{"text": "Give your best final answer now."}]})
        return self._text(self._converse(system=system_blocks, messages=messages, inferenceConfig={"temperature": 0.1})["output"]["message"])

    def complete(self, system: str, prompt: str) -> str:
        resp = self._converse(system=[{"text": system}], messages=[{"role": "user", "content": [{"text": prompt}]}],
                              inferenceConfig={"temperature": 0.0})
        return self._text(resp["output"]["message"])


# --------------------------------------------------------------------------- CLI / command
_TOOL_LINE = re.compile(r"\s*TOOL\s+(\w+)\s+(\{.*\})\s*$", re.S)


def _tool_protocol_instructions(toolbox: Any) -> str:
    lines = ["You can use tools. To call one, reply with EXACTLY one line and nothing else:",
             "TOOL <tool_name> <json-arguments>", "Available tools:"]
    for t in _tool_specs(toolbox):
        fn = t["function"]
        lines.append(f"- {fn['name']}: {fn['description']} Arguments schema: {json.dumps(fn['parameters'])}")
    lines.append("After each TOOL_RESULT, call another tool or reply with your final plain-text answer.")
    return "\n".join(lines)


class CommandLLM:
    """Runs a configured command per turn: full prompt on stdin, completion on stdout."""

    supports_tools = False
    provider = "cli"

    def __init__(self, command: str, timeout: int = 120, is_local: bool = False):
        self.command = command
        self.timeout = timeout
        self.is_local = is_local  # set by the operator: a CLI may call a cloud service
        self.model = command.split()[0] if command.split() else "cli"
        self.name = f"cli:{self.model.rsplit('/', 1)[-1]}"

    def _run(self, prompt: str) -> str:
        try:
            proc = subprocess.run(self.command, shell=True, input=prompt, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise LLMError("cli: timed out") from exc
        if proc.returncode != 0:
            raise LLMError(f"cli: exited with code {proc.returncode}")
        return proc.stdout.strip()

    @staticmethod
    def _render(system: str, turns: list[dict[str, str]], question: str) -> str:
        parts = [f"[SYSTEM]\n{system}"]
        parts += [f"[{t['role'].upper()}]\n{t['content']}" for t in turns]
        parts += [f"[USER]\n{question}", "[ASSISTANT]"]
        return "\n\n".join(parts)

    def converse(self, system, history, question, toolbox, max_iters, stream=None):
        return _emit_once(stream, self._converse_all(system, history, question, toolbox, max_iters))

    def _converse_all(self, system, history, question, toolbox, max_iters):
        specs = _tool_specs(toolbox)
        if specs:
            system = system + "\n\n" + _tool_protocol_instructions(toolbox)
        turns = list(history)
        for _ in range(max_iters):
            out = self._run(self._render(system, turns, question))
            m = _TOOL_LINE.match(out)
            if not m or not specs:
                return out
            try:
                args = json.loads(m.group(2))
            except json.JSONDecodeError:
                args = {"__invalid_json__": m.group(2)}
            result = toolbox.run(m.group(1), args)
            turns += [{"role": "assistant", "content": out},
                      {"role": "user", "content": f"TOOL_RESULT[{m.group(1)}]: {json.dumps(result, default=str)[:4000]}"}]
        return self._run(self._render(system + "\nGive your final answer now.", turns, question))

    def complete(self, system: str, prompt: str) -> str:
        return self._run(self._render(system, [], prompt))
