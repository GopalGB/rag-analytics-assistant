"""LLM provider selection + the Bedrock and CLI tool-calling loops (no real cloud/AWS needed)."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from app.agent.llm import BedrockLLM, CommandLLM, OpenAILLM, ProviderRateLimitError, build_llm
from app.agent.tools import ToolBox
from app.config import Settings


class _StubBedrockClient:
    """Returns a toolUse on the first turn, then a final text answer."""

    def __init__(self):
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "t1",
                                    "name": "run_sql",
                                    "input": {
                                        "sql": "SELECT region, sum(revenue) AS total FROM sales GROUP BY region"
                                    },
                                }
                            }
                        ],
                    }
                }
            }
        return {
            "usage": {"inputTokens": 20, "outputTokens": 7, "totalTokens": 27},
            "output": {"message": {"role": "assistant", "content": [{"text": "South 200, North 150."}]}},
        }


def test_bedrock_tool_loop(store, retriever):
    llm = BedrockLLM("model-x", "us-east-1", client=_StubBedrockClient())
    out = llm.converse("system", [], "which region has the most revenue?", ToolBox(store, retriever), 4)
    assert "200" in out


def test_bedrock_records_sql(store, retriever):
    tb = ToolBox(store, retriever)
    llm = BedrockLLM("model-x", "us-east-1", client=_StubBedrockClient())
    llm.converse("s", [], "q", tb, 4)
    assert tb.last_sql and "sales" in tb.last_sql.lower()
    assert llm.request_usage() == {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42}


def test_command_llm_text_protocol(tmp_path, store, retriever):
    # A tiny CLI that emits a TOOL line first, then a final answer once it sees a TOOL_RESULT.
    script = tmp_path / "cli.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        'if "TOOL_RESULT[" in data:\n'
        '    print("final answer ready")\n'
        "else:\n"
        '    print(\'TOOL run_sql {"sql": "SELECT region FROM sales"}\')\n'
    )
    llm = CommandLLM(f"{sys.executable} {script}")
    tb = ToolBox(store, retriever)
    out = llm.converse("system", [], "list the regions", tb, 4)
    assert "final answer" in out
    assert tb.last_sql and "sales" in tb.last_sql.lower()


def test_command_llm_keeps_arguments_after_quoted_executable_with_space(tmp_path):
    # A quoted executable path containing a space must not swallow the arguments after it.
    folder = tmp_path / "my tools"
    folder.mkdir()
    script = folder / "cli"
    script.write_text('#!/bin/sh\necho "$@"\n')
    script.chmod(0o755)
    llm = CommandLLM(f'"{script}" --flag value')
    assert llm._run("prompt") == "--flag value"


def test_openai_rate_limit_is_typed_and_only_exposes_numeric_retry_after(monkeypatch):
    class RateLimitedResponse:
        status_code = 429
        headers = {"retry-after": "17"}

        def raise_for_status(self):
            raise requests.HTTPError("provider message", response=self)

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: RateLimitedResponse())
    with pytest.raises(ProviderRateLimitError) as excinfo:
        OpenAILLM("key", "https://provider.test/v1", "model")._call([], None)
    assert excinfo.value.retry_after == 17
    assert str(excinfo.value) == "model provider is rate limited"


def test_openai_caps_completion_tokens_in_each_provider_request(monkeypatch):
    sent: list[dict] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "answer"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    def post(*_args, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(requests, "post", post)
    assert OpenAILLM("key", "https://provider.test/v1", "model")._call([], None)["content"] == "answer"
    assert sent == [{"model": "model", "messages": [], "temperature": 0.1, "max_completion_tokens": 1024}]


def test_openai_usage_is_isolated_per_concurrent_request(monkeypatch, store, retriever):
    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, token: int):
            self.token = token

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": f"answer-{self.token}"}}],
                "usage": {"prompt_tokens": self.token, "completion_tokens": 1, "total_tokens": self.token + 1},
            }

    def post(*_args, **kwargs):
        question = kwargs["json"]["messages"][-1]["content"]
        return Response(int(question.rsplit("-", 1)[1]))

    monkeypatch.setattr(requests, "post", post)
    llm = OpenAILLM("key", "https://provider.test/v1", "model")

    def invoke(token: int):
        answer = llm.converse("system", [], f"question-{token}", ToolBox(store, retriever), 1)
        return answer, llm.request_usage()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, (7, 13)))
    assert sorted(results) == [
        ("answer-13", {"input_tokens": 13, "output_tokens": 1, "total_tokens": 14}),
        ("answer-7", {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8}),
    ]

def test_build_llm_selection():
    # Pin provider fields explicitly so an ambient OPENAI_API_KEY in the environment can't leak in.
    base = {"openai_api_key": None, "bedrock_model_id": None, "llm_cli_command": None}
    assert build_llm(Settings(llm_provider="none", **base)) is None
    assert build_llm(Settings(llm_provider="openai", **base)) is None  # no key configured
    assert isinstance(
        build_llm(Settings(llm_provider="openai", **{**base, "openai_api_key": "k"})), OpenAILLM
    )
    assert isinstance(
        build_llm(Settings(llm_provider="cli", **{**base, "llm_cli_command": "echo hi"})), CommandLLM
    )
    assert isinstance(build_llm(Settings(llm_provider="auto", **{**base, "openai_api_key": "k"})), OpenAILLM)
    assert isinstance(
        build_llm(Settings(llm_provider="auto", **{**base, "llm_cli_command": "echo hi"})), CommandLLM
    )
