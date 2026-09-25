"""Providers (Anthropic native, OpenAI-compatible family, Azure) and the model registry, with fake HTTP."""

from __future__ import annotations

import json

import pytest

from app.agent.tools import ToolBox
from app.config import Settings
from app.llm import usage
from app.llm.providers import AnthropicLLM, AzureOpenAILLM, LLMError, OpenAICompatLLM, is_local_url
from app.llm.registry import CLOUD_BLOCKED, build_chain, build_model, default_chain, parse_spec


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


NO_KEYS = {k: None for k in ("openai_api_key", "anthropic_api_key", "gemini_api_key", "openrouter_api_key",
                             "groq_api_key", "mistral_api_key", "deepseek_api_key", "together_api_key", "xai_api_key",
                             "azure_openai_api_key", "bedrock_model_id", "llm_cli_command", "ollama_model",
                             "llm_models_fast", "llm_models_strong")}


def S(**kw) -> Settings:
    return Settings(**{**NO_KEYS, "llm_provider": "auto", "llm_cli_is_local": False, **kw})


# --------------------------------------------------------------------------- Anthropic
def test_anthropic_tool_loop(store, retriever):
    http = FakeHTTP(
        Resp({"content": [{"type": "text", "text": "Let me check."},
                          {"type": "tool_use", "id": "tu1", "name": "run_sql",
                           "input": {"sql": "SELECT region, sum(revenue) AS t FROM sales GROUP BY region"}}],
              "usage": {"input_tokens": 100, "output_tokens": 20}}),
        Resp({"content": [{"type": "text", "text": "South leads with 200."}], "usage": {"input_tokens": 150, "output_tokens": 10}}),
    )
    llm = AnthropicLLM("sk-test", "claude-sonnet-5", http=http)
    tb = ToolBox(store, retriever)
    with usage.capture() as u:
        out = llm.converse("sys", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                           "who leads?", tb, 4)
    assert out == "South leads with 200."
    assert (u.input_tokens, u.output_tokens) == (250, 30)
    first, second = http.requests
    assert first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == "sk-test" and first["headers"]["anthropic-version"] == "2023-06-01"
    assert first["json"]["system"] == "sys" and first["json"]["model"] == "claude-sonnet-5"
    assert {t["name"] for t in first["json"]["tools"]} == {"run_sql", "search_docs"}
    assert "input_schema" in first["json"]["tools"][0]
    result_block = second["json"]["messages"][-1]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "tu1"
    assert tb.last_sql and "sales" in tb.last_sql


def test_anthropic_structured_output_uses_tool_forcing():
    http = FakeHTTP(Resp({"content": [{"type": "tool_use", "id": "x", "name": "emit_routedecision",
                                       "input": {"intent": "documents", "confidence": 0.8}}]}))
    out = AnthropicLLM("k", "m", http=http).complete_json("s", "p", {"type": "object"}, "RouteDecision")
    assert json.loads(out)["intent"] == "documents"
    body = http.requests[0]["json"]
    assert body["tool_choice"] == {"type": "tool", "name": "emit_routedecision"}


def test_http_errors_are_typed():
    http = FakeHTTP(Resp({"error": {"message": "bad key"}}, 401))
    with pytest.raises(LLMError) as e:
        AnthropicLLM("k", "m", http=http).complete("s", "p")
    assert e.value.status == 401 and "authentication" in str(e.value)


# --------------------------------------------------------------------------- OpenAI-compatible
def test_openai_compat_tool_loop_and_json_mode(store, retriever):
    http = FakeHTTP(
        Resp({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_docs", "arguments": '{"query": "baseline"}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        Resp({"choices": [{"message": {"content": "Baseline means expected units [guide.md]."}}]}),
        Resp({"choices": [{"message": {"content": '{"intent": "general", "confidence": 0.5}'}}]}),
    )
    llm = OpenAICompatLLM("gk", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", provider="groq", http=http)
    assert llm.name == "groq:llama-3.3-70b-versatile" and not llm.is_local
    tb = ToolBox(store, retriever, allowed_tools=("search_docs",))
    assert "guide.md" in llm.converse("sys", [], "what is baseline?", tb, 3)
    assert http.requests[0]["headers"]["Authorization"] == "Bearer gk"
    assert [t["function"]["name"] for t in http.requests[0]["json"]["tools"]] == ["search_docs"]
    assert http.requests[1]["json"]["messages"][-1]["role"] == "tool"
    llm.complete_json("s", "p", {"type": "object"})
    assert http.requests[2]["json"]["response_format"] == {"type": "json_object"}


def test_json_mode_falls_back_when_unsupported():
    http = FakeHTTP(Resp({"error": "response_format not supported"}, 400),
                    Resp({"choices": [{"message": {"content": "{}"}}]}))
    llm = OpenAICompatLLM("k", "https://openrouter.ai/api/v1", "x/y", provider="openrouter", http=http)
    assert llm.complete_json("s", "p", {}) == "{}"
    assert "response_format" not in http.requests[1]["json"]


def test_azure_url_and_header():
    http = FakeHTTP(Resp({"choices": [{"message": {"content": "ok"}}]}))
    llm = AzureOpenAILLM("azk", "https://res.openai.azure.com", "gpt4o-deploy", "2024-10-21", http=http)
    assert llm.complete("s", "p") == "ok"
    req = http.requests[0]
    assert req["url"] == "https://res.openai.azure.com/openai/deployments/gpt4o-deploy/chat/completions?api-version=2024-10-21"
    assert req["headers"]["api-key"] == "azk" and "Authorization" not in req["headers"]


def test_local_url_detection():
    assert is_local_url("http://127.0.0.1:11434/v1") and is_local_url("http://localhost:1234/v1")
    assert is_local_url("http://192.168.1.20:11434/v1")  # on-premises model server
    assert not is_local_url("https://api.openai.com/v1")


# --------------------------------------------------------------------------- registry
def test_parse_spec_keeps_colons_in_model():
    assert parse_spec("ollama:qwen2.5:14b") == ("ollama", "qwen2.5:14b")
    assert parse_spec("openrouter:meta-llama/llama-3.3-70b-instruct")[1] == "meta-llama/llama-3.3-70b-instruct"
    with pytest.raises(ValueError):
        parse_spec("nope:model")


@pytest.mark.parametrize(
    "spec,key,expected_url",
    [
        ("gemini:gemini-2.5-flash", "gemini_api_key", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("openrouter:openai/gpt-4o", "openrouter_api_key", "https://openrouter.ai/api/v1"),
        ("groq:llama-3.3-70b-versatile", "groq_api_key", "https://api.groq.com/openai/v1"),
        ("mistral:mistral-large-latest", "mistral_api_key", "https://api.mistral.ai/v1"),
        ("deepseek:deepseek-chat", "deepseek_api_key", "https://api.deepseek.com/v1"),
        ("together:meta-llama/Llama-3.3-70B-Instruct-Turbo", "together_api_key", "https://api.together.xyz/v1"),
        ("xai:grok-model", "xai_api_key", "https://api.x.ai/v1"),
    ],
)
def test_every_provider_builds_from_its_key(spec, key, expected_url):
    llm, note = build_model(spec, S(allow_cloud_ai=True, **{key: "k"}))
    assert llm is not None, note
    assert llm.base_url == expected_url and llm.name == spec
    blocked, note = build_model(spec, S(allow_cloud_ai=False, **{key: "k"}))
    assert blocked is None and "explicit approval" in note
    missing, note = build_model(spec, S(allow_cloud_ai=True))
    assert missing is None and key.upper() in note


def test_anthropic_and_azure_build():
    llm, _ = build_model("anthropic:claude-sonnet-5", S(allow_cloud_ai=True, anthropic_api_key="k"))
    assert isinstance(llm, AnthropicLLM)
    llm, note = build_model("azure:dep", S(allow_cloud_ai=True, azure_openai_api_key="k"))
    assert llm is None and "ENDPOINT" in note
    llm, _ = build_model("azure:dep", S(allow_cloud_ai=True, azure_openai_api_key="k", azure_openai_endpoint="https://r.openai.azure.com"))
    assert isinstance(llm, AzureOpenAILLM)


def test_default_chains_follow_keys_and_preference():
    s = S(allow_cloud_ai=True, anthropic_api_key="a", openai_api_key="o")
    assert default_chain(s, "strong") == ["anthropic:claude-sonnet-5", "openai:gpt-4o"]
    assert default_chain(s, "fast") == ["anthropic:claude-haiku-4-5-20251001", "openai:gpt-4o-mini"]
    local_first = S(allow_cloud_ai=True, openai_api_key="o", ollama_model="qwen2.5:14b", llm_provider="ollama")
    assert default_chain(local_first, "fast") == ["ollama:qwen2.5:14b"]  # provider pinned


def test_explicit_chain_overrides_defaults_and_skips_unusable():
    s = S(allow_cloud_ai=True, openai_api_key="o",
          llm_models_strong="anthropic:claude-sonnet-5, openai:gpt-4o, bogus:x")
    models, notes = build_chain(s, "strong")
    assert [m.name for m in models] == ["openai:gpt-4o"]
    assert any("ANTHROPIC_API_KEY" in n for n in notes) and any("invalid model spec" in n for n in notes)


def test_cloud_blocked_message_mentions_approval():
    assert "ALLOW_CLOUD_AI" in CLOUD_BLOCKED
