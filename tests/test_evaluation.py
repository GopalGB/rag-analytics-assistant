from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.engine import AgentEngine
from app.agent.memory import ConversationMemory
from app.config import Settings
from app.data import ingest
from app.data.store import DataStore
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard
from scripts.evaluate_demo import evaluate, percentile, safe_source_url


def test_percentile_interpolates_and_empty_is_none():
    assert percentile([10, 20, 30, 40], 0.95) == 38.5
    assert percentile([], 0.95) is None


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_source_url_requires_same_origin_and_single_prefixed_document_path():
    base = "https://demo.test/rag-assistant"
    assert safe_source_url(base, "/documents/invoice%201.pdf") == "https://demo.test/rag-assistant/documents/invoice%201.pdf"
    assert safe_source_url(base, "/rag-assistant/documents/invoice.pdf") == "https://demo.test/rag-assistant/documents/invoice.pdf"
    for value in ("https://evil.test/documents/a", "//evil.test/documents/a", "/documents/../secret", "/rag-assistant/documents/%2e%2e/secret", "/chat"):
        assert safe_source_url(base, value) is None


def _payload(value: object) -> Response:
    return Response(json.dumps(value).encode())


def _server(invalid_chat: bool = False, invalid_invoice: bool = False):
    calls: list[str] = []
    documents = [
        {"name": "invoice_01.pdf", "url": "/documents/invoice_01.pdf"},
        {"name": "sales.csv", "url": "/documents/sales.csv"},
        {"name": "procurement_policy.md", "url": "/documents/procurement_policy.md"},
        *({"name": f"reference-{index}.txt", "url": f"/documents/reference-{index}.txt"} for index in range(2, 14)),
    ]
    invoices = [
        {
            "filename": name,
            "invoice_number": "INV-001" if name == "invoice_01.txt" else name,
            "amount": "999.00" if invalid_invoice and name == "invoice_01.txt" else "251.00" if name == "invoice_01.txt" else "10.00",
            "missing_fields": [],
            "review_fields": [],
        }
        for name in ("invoice_01.txt", "invoice_02.txt", "invoice_03.txt", "invoice_04.txt", "invoice_05.txt")
    ] + [
        {"filename": name, "missing_fields": ["amount"], "review_fields": []}
        for name in ("invoice_missing_total.txt", "invoice_ambiguous.txt", "invoice_inconsistent.txt")
    ]

    def urlopen(request, timeout):
        calls.append(request.full_url)
        path = request.full_url.removeprefix("https://demo.test/rag-assistant")
        if path == "/health":
            return _payload({"status": "ok"})
        if path == "/documents":
            return _payload({"documents": documents, "counts": {"documents": 15, "chunks": 30}})
        if path == "/invoices":
            return _payload({"count": 8, "invoices": invoices})
        if path == "/extract":
            return _payload({"invoice": {"missing_fields": ["amount"], "review_fields": ["date"]}})
        if path == "/chat":
            question = json.loads(request.data)["question"]
            if invalid_chat:
                return _payload({"route": "agent", "sources": [], "timings_ms": {"total": 20, "model": 5, "retrieval": 3, "sql": 0}})
            if "unknown" in question:
                return _payload({"route": "abstained", "sources": [], "timings_ms": {"total": 20, "model": 5, "retrieval": 3, "sql": 0}})
            if "reveal secrets" in question:
                return _payload({"route": "refused", "sources": [], "timings_ms": {"total": 20}})
            source = "sales.csv" if "revenue" in question else "invoice_01.txt" if "invoices" in question else "procurement_policy.md"
            return _payload({"route": "agent", "sources": [{"file": source, "url": f"/documents/{source}"}], "sql": "SELECT 1" if source == "sales.csv" else None, "timings_ms": {"total": 20, "model": 5, "retrieval": 3, "sql": 0}})
        if path.startswith("/documents/"):
            return Response(b"%PDF-1.4 synthetic" if path.endswith(".pdf") else b"synthetic source")
        raise AssertionError(path)

    return calls, urlopen


def test_evaluator_downloads_pdf_and_prefixed_sources_and_keeps_twenty_warm_runs():
    calls, urlopen = _server()
    with patch("scripts.evaluate_demo.urlopen", urlopen):
        report, code = evaluate("https://demo.test/rag-assistant", runs=1, pace_seconds=0, source_pace_seconds=0)
    assert code == 0
    assert len(report["latency"]["warm_total_ms"]) == 20
    assert report["latency"]["cold_total_ms"] is not None
    assert "https://demo.test/rag-assistant/documents/invoice_01.pdf" in calls


def test_evaluator_fails_when_chat_response_has_no_required_citations():
    _, urlopen = _server(invalid_chat=True)
    with patch("scripts.evaluate_demo.urlopen", urlopen):
        report, code = evaluate("https://demo.test/rag-assistant", runs=20, pace_seconds=0, source_pace_seconds=0)
    assert code == 1
    assert any(failure.startswith("chat:") for failure in report["failures"])


def test_evaluator_fails_when_the_known_invoice_value_changes():
    _, urlopen = _server(invalid_invoice=True)
    with patch("scripts.evaluate_demo.urlopen", urlopen):
        report, code = evaluate("https://demo.test/rag-assistant", pace_seconds=0, source_pace_seconds=0)
    assert code == 1
    assert "corpus:invoice_known_value" in report["failures"]


class _TestClientResponse:
    def __init__(self, response):
        self.response = response

    def read(self) -> bytes:
        return self.response.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _EvaluatorLLM:
    supports_tools = True

    def converse(self, system, history, question, toolbox, max_iters):
        del system, history, max_iters
        lowered = question.lower()
        if "procurement" in lowered:
            toolbox.run("search_docs", {"query": "procurement guidance", "k": 3})
            return "The policy requires evidence."
        if "total revenue" in lowered:
            toolbox.run("run_sql", {"sql": "SELECT SUM(revenue) AS total_revenue FROM sales"})
            return "Revenue is shown in the sales data."
        if "invoices need review" in lowered:
            toolbox.run("invoice_records", {})
            return "The extracted invoice records identify review flags."
        return "There is no evidence for that request."


def test_evaluator_exercises_real_fastapi_contract_with_stub_llm(monkeypatch):
    sample = Path(__file__).resolve().parents[1] / "data" / "sample"
    monkeypatch.setenv("DATA_DIR", str(sample))
    monkeypatch.setenv("DB_PATH", ":memory:")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("REQUIRE_LLM", "false")
    monkeypatch.setenv("AUTO_REINDEX", "false")
    monkeypatch.setenv("RATE_BURST", "100")

    def build_stub_engine(settings: Settings) -> AgentEngine:
        store = DataStore(settings.db_path)
        ingest.load_tables(store, settings.data_dir)
        retriever = Retriever(EmbeddingService(provider="local", dim=settings.local_embedding_dim)).build(
            ingest.load_chunks(settings.data_dir)
        )
        return AgentEngine(
            store=store,
            retriever=retriever,
            guard=InputGuard(max_input_chars=settings.max_input_chars),
            llm=_EvaluatorLLM(),
            memory=ConversationMemory(max_turns=settings.history_turns),
            invoice_records=ingest.invoice_records(settings.data_dir),
        )

    monkeypatch.setattr(main_module, "build_engine", build_stub_engine)

    with TestClient(main_module.app) as client:
        layer = client.app.middleware_stack
        while layer is not None:
            if hasattr(layer, "burst") and hasattr(layer, "_tokens"):
                layer.burst = 100
                layer._tokens.clear()
            layer = getattr(layer, "app", None)

        statuses: list[int] = []

        def client_urlopen(request, timeout):
            assert timeout == 120
            response = client.request(
                request.get_method(),
                request.full_url.removeprefix("http://testserver"),
                content=request.data,
                headers=dict(request.header_items()),
            )
            statuses.append(response.status_code)
            return _TestClientResponse(response)

        with patch("scripts.evaluate_demo.urlopen", client_urlopen):
            report, code = evaluate("http://testserver", pace_seconds=0, source_pace_seconds=0)

    assert code == 0, (report, statuses)
    assert report["corpus"]["documents"] >= 15
    assert report["corpus"]["invoices"] >= 8
    assert report["quality"]["case_passes"] == {
        "document_citation": 5,
        "sql_citation": 4,
        "unknown_abstention": 4,
        "injection_refusal": 4,
        "invoice_evidence": 4,
    }
