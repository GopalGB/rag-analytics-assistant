"""Shared fixtures: a tiny synthetic dataset, a built store/retriever, and a fake LLM."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.agent.llm import LLMResponse, ToolCall
from app.data import ingest
from app.data.store import DataStore
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    with (d / "sales.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["region", "product", "revenue"])
        w.writerow(["North", "Lamp", 100])
        w.writerow(["North", "Mug", 50])
        w.writerow(["South", "Lamp", 200])
    (d / "guide.md").write_text(
        "# Guide\nBaseline means expected units sold under normal conditions. "
        "Lift is the incremental units from a promotion.",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def store(tmp_path: Path, data_dir: Path) -> DataStore:
    s = DataStore(str(tmp_path / "test.duckdb"))
    ingest.load_tables(s, str(data_dir))
    yield s
    s.close()


@pytest.fixture
def retriever(data_dir: Path) -> Retriever:
    emb = EmbeddingService(provider="local", dim=128)
    return Retriever(emb).build(ingest.load_chunks(str(data_dir)))


class FakeLLM:
    """Scripted LLM: emits the queued responses in order. Lets tests drive the tool loop."""

    def __init__(self, script: list[LLMResponse]):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools):
        self.calls.append(messages)
        return self.script.pop(0) if self.script else LLMResponse(content="done")


@pytest.fixture
def fake_tool_then_answer() -> FakeLLM:
    return FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="run_sql",
                        arguments={
                            "sql": "SELECT region, sum(revenue) AS total FROM sales GROUP BY region ORDER BY total DESC"
                        },
                    )
                ]
            ),
            LLMResponse(
                content="South leads with 200 in revenue, then North with 150."
            ),
        ]
    )
