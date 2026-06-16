"""LLM provider selection + the Bedrock and CLI tool-calling loops (no real cloud/AWS needed)."""

from __future__ import annotations

import sys

from app.agent.llm import BedrockLLM, CommandLLM, OpenAILLM, build_llm
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
        return {"output": {"message": {"role": "assistant", "content": [{"text": "South 200, North 150."}]}}}


def test_bedrock_tool_loop(store, retriever):
    llm = BedrockLLM("model-x", "us-east-1", client=_StubBedrockClient())
    out = llm.converse("system", [], "which region has the most revenue?", ToolBox(store, retriever), 4)
    assert "200" in out


def test_bedrock_records_sql(store, retriever):
    tb = ToolBox(store, retriever)
    BedrockLLM("model-x", "us-east-1", client=_StubBedrockClient()).converse("s", [], "q", tb, 4)
    assert tb.last_sql and "sales" in tb.last_sql.lower()


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
