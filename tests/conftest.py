"""Shared fixtures: a tiny synthetic dataset, a built store/retriever, and a fake LLM."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.config import Settings
from app.data import ingest
from app.data.store import DataStore
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever

# A developer's .env (provider keys, PUBLIC_DEMO, ...) must not leak into tests: switch dotenv loading off
# before any test module imports app.main and builds its Settings. Environment variables still apply, and
# the fixture below clears the provider ones.
Settings.model_config["env_file"] = None

_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "BEDROCK_MODEL_ID",
    "LLM_CLI_COMMAND",
    "LLM_PROVIDER",
    "LLM_MODELS_STRONG",
    "LLM_MODELS_FAST",
    "ALLOW_CLOUD_AI",
    "EMBEDDING_API_KEY",
    "PUBLIC_DEMO",
    "APP_API_KEY",
)


@pytest.fixture(autouse=True)
def _hermetic_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's shell (provider keys, routing overrides) from changing test outcomes."""
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


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
    """Implements the converse() interface: optionally fires one SQL tool call, then answers."""

    supports_tools = True

    def __init__(self, sql: str | None = None, answer: str = "done"):
        self.sql = sql
        self.answer = answer

    name = "fake"
    is_local = True

    def converse(self, system, history, question, toolbox, max_iters):
        if self.sql:
            toolbox.run("run_sql", {"sql": self.sql})
        return self.answer

    def complete(self, system, prompt):
        return "{}"


@pytest.fixture
def fake_tool_then_answer() -> FakeLLM:
    return FakeLLM(
        sql="SELECT region, sum(revenue) AS total FROM sales GROUP BY region ORDER BY total DESC",
        answer="South leads with 200 in revenue, then North with 150.",
    )
