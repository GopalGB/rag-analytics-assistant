"""Features ported from the 2026-09 update branch: read-only public demo mode, bounded memory, SQL work
limits, streamed body limits, bounded rate-limit state, shell-free CLI models, more invoice layouts
(plain text, AED, labelled quantity/unit price, unreadable totals) and the Cloudflare proxy config."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.memory import ConversationMemory
from app.config import Settings
from app.data.store import DataStore, UnsafeQueryError
from app.documents.ocr import OCREngine
from app.documents.parsers import parse_file
from app.invoices.extract import extract_invoice
from app.llm.providers import CommandLLM, command_argv
from app.middleware import BodyLimitMiddleware, RateLimitMiddleware, install_security_middleware
from app.security.output_filter import scrub

ROOT = Path(__file__).resolve().parent.parent
LAYOUTS = ROOT / "data" / "unseen_invoices" / "more_layouts"


# --------------------------------------------------------------------------- public demo
def _demo_client(**kw) -> TestClient:
    app = FastAPI()

    @app.post("/chat")
    def chat() -> dict:
        return {"ok": True}

    @app.post("/extract")
    def extract() -> dict:
        return {"ok": True}

    for path in ("/upload", "/refresh", "/qbo/sync", "/qbo/disconnect", "/approvals", "/invoices/x/review",
                 "/approvals/x/decision"):
        app.add_api_route(path, lambda: {"ok": True}, methods=["POST"])
    app.add_api_route("/qbo/connect", lambda: {"ok": True}, methods=["GET"])
    install_security_middleware(app, Settings(**{"allowed_hosts": "testserver", "rate_burst": 100, **kw}))
    return TestClient(app)


def test_public_demo_is_read_only():
    demo = _demo_client(public_demo=True)
    for path in ("/upload", "/refresh", "/qbo/sync", "/qbo/disconnect", "/approvals", "/invoices/x/review",
                 "/approvals/x/decision"):
        r = demo.post(path)
        assert r.status_code == 403 and "read-only public demo" in r.json()["error"], path
    assert demo.get("/qbo/connect").status_code == 403
    assert demo.post("/chat").status_code == 200 and demo.post("/extract").status_code == 200
    normal = _demo_client()
    assert normal.post("/upload").status_code == 200


def test_proxy_origin_can_be_allowed():
    c = _demo_client(allowed_origins="https://example.com")
    assert c.post("/chat", headers={"Origin": "https://example.com"}).status_code == 200
    assert c.post("/chat", headers={"Origin": "https://evil.example"}).status_code == 403


def test_memory_can_be_off_and_is_bounded():
    off = ConversationMemory(max_turns=0)
    off.add("s", "user", "hello")
    assert off.history("s") == [] and off.session_count() == 0
    mem = ConversationMemory(max_turns=2, max_sessions=3)
    for i in range(5):
        mem.add(f"s{i}", "user", "q")
    assert mem.session_count() == 3 and mem.history("s0") == [] and mem.history("s4")
    mem.history("never-seen")
    assert mem.session_count() == 3  # reading doesn't create sessions


# --------------------------------------------------------------------------- SQL work limits
@pytest.fixture
def sql_store(tmp_path):
    s = DataStore(str(tmp_path / "s.duckdb"), query_timeout_seconds=0.3)
    s.load_dataframe("sales", pd.DataFrame({"v": range(200)}))
    yield s
    s.close()


@pytest.mark.parametrize("sql,message", [
    ("SELECT * FROM range(1000000000000)", "table function"),
    ("SELECT * FROM generate_series(1, 10)", "table function"),
    ("SELECT * FROM query_table('sales')", "table function"),
    ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t", "recursive"),
    ("SELECT getenv('HOME')", "getenv"),
    ("SELECT current_setting('memory_limit')", "current_setting"),
])
def test_row_generators_and_settings_are_blocked(sql_store, sql, message):
    with pytest.raises(UnsafeQueryError, match=message):
        sql_store.run_select(sql)


def test_slow_query_is_interrupted_and_store_still_works(sql_store):
    with pytest.raises(UnsafeQueryError, match="time limit"):
        sql_store.run_select("SELECT count(*) FROM sales a, sales b, sales c, sales d, sales e")
    assert sql_store.run_select("SELECT sum(v) FROM sales")[1] == [(19900,)]
    assert len(sql_store.run_select("SELECT * FROM sales", max_rows=10_000)[1]) == 200  # row cap clamps


def test_in_memory_database_and_bad_settings():
    DataStore(":memory:").close()
    with pytest.raises(ValueError):
        DataStore(":memory:", query_timeout_seconds=0)
    with pytest.raises(ValueError):
        DataStore(":memory:", memory_limit="1GB'; DROP")


# --------------------------------------------------------------------------- middleware
def _run_asgi(app, messages):
    sent, calls = [], []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    asyncio.run(app(scope, receive, send))
    return sent, calls


def test_chunked_body_over_limit_is_rejected_without_reaching_the_app():
    reached = []

    async def inner(scope, receive, send):
        reached.append(await receive())

    mw = BodyLimitMiddleware(inner, max_bytes=4)
    sent, _ = _run_asgi(mw, [{"type": "http.request", "body": b"abc", "more_body": True},
                             {"type": "http.request", "body": b"de", "more_body": False}])
    assert sent[0]["status"] == 413 and not reached


def test_body_is_replayed_then_real_receive_is_used():
    seen = []

    async def inner(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())  # a streaming response listening for the client going away

    mw = BodyLimitMiddleware(inner, max_bytes=10)
    _run_asgi(mw, [{"type": "http.request", "body": b"ab", "more_body": True},
                   {"type": "http.request", "body": b"cd", "more_body": False}])
    assert seen[0]["body"] == b"abcd" and seen[1]["type"] == "http.disconnect"


def test_rate_limit_state_is_bounded():
    rl = RateLimitMiddleware(FastAPI(), per_minute=60, burst=2, max_clients=3, ttl=300)
    now = 1000.0
    for i in range(10):
        if f"ip{i}" not in rl._state and len(rl._state) >= rl.max_clients:
            rl._evict(now + i)
        rl._state[f"ip{i}"] = (1.0, now + i)
    assert len(rl._state) <= 3 and "ip9" in rl._state


# --------------------------------------------------------------------------- CLI model without a shell
def test_cli_command_runs_without_a_shell(tmp_path):
    folder = tmp_path / "my tools"
    folder.mkdir()
    script = folder / "echo model.py"
    script.write_text("import sys; print('got:' + sys.stdin.read()[-10:].strip() + ' ' + ' '.join(sys.argv[1:]))")
    argv = command_argv(f"{sys.executable} '{script}' --flag value")
    assert argv == [sys.executable, str(script), "--flag", "value"]
    assert command_argv(f"{script} --x")[0] == str(script)  # unquoted path with spaces is recovered
    out = CommandLLM(f"{sys.executable} '{script}' --flag value").complete("s", "hello; rm -rf /")
    assert out.startswith("got:") and "--flag value" in out


def test_groq_keys_are_scrubbed():
    assert "gsk_" not in scrub("key gsk_abc-DEF_1234567890xyz leaked")


# --------------------------------------------------------------------------- more invoice layouts
EXPECTED = {
    "invoice_01.txt": ("Fictional Vendor 1", "INV-001", "2026-10-01", 251.0, "USD"),
    "invoice_02.txt": ("Fictional Vendor 2", "INV-002", "2026-10-02", 376.5, "USD"),
    "invoice_03.txt": ("Fictional Vendor 3", "INV-003", "2026-10-03", 502.0, "USD"),
    "invoice_04.txt": ("Fictional Vendor 4", "INV-004", "2026-10-04", 627.5, "USD"),
    "invoice_05.txt": ("Fictional Vendor 5", "INV-005", "2026-10-05", 753.0, "USD"),
    "invoice_pdf_001.pdf": ("Fictional PDF Vendor 1", "INV-PDF001", "2026-09-01", 105.0, "AED"),
    "invoice_pdf_002.pdf": ("Fictional PDF Vendor 2", "INV-PDF002", "2026-09-02", 210.0, "AED"),
    "invoice_pdf_003.pdf": ("Fictional PDF Vendor 3", "INV-PDF003", "2026-09-03", 315.0, "AED"),
}


def _extract(name):
    doc = parse_file(LAYOUTS / name, name, OCREngine())
    return extract_invoice(doc.text, ocr=doc.used_ocr, warnings=doc.warnings)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_clean_layouts_extract_completely(name):
    ex = _extract(name)
    got = tuple(ex.value(f) for f in ("supplier", "invoice_number", "invoice_date", "total", "currency"))
    assert got == EXPECTED[name] and ex.issues == []


def test_flawed_layouts_are_flagged_not_guessed():
    missing = _extract("invoice_missing_total.txt")
    assert missing.value("total") is None  # 'Total: MISSING' — never the unit price instead
    assert any("can't be read ('Total: MISSING')" in i for i in missing.issues)
    ambiguous = _extract("invoice_ambiguous.txt")
    assert ambiguous.value("total") is None and any("Quantity 'two' is not a number" in i for i in ambiguous.issues)
    inconsistent = _extract("invoice_inconsistent.txt")
    assert inconsistent.value("invoice_number") == "INV-INCONSISTENT"
    assert any("3 x unit price 10.00 = 30.00, but the total is 25.00" in i for i in inconsistent.issues)


def test_public_documents_are_documents_not_invoices():
    from app.invoices.registry import is_invoice_document

    folder = ROOT / "data" / "sample" / "documents" / "public"
    for f in folder.iterdir():
        doc = parse_file(f, f"documents/public/{f.name}", OCREngine())
        assert len(doc.text) > 1000 and not is_invoice_document(doc), f.name


# --------------------------------------------------------------------------- proxy config
def test_worker_config_and_node_tests_exist():
    assert (ROOT / "deploy" / "rag-assistant-worker.mjs").exists()
    toml = (ROOT / "deploy" / "wrangler.toml").read_text()
    assert 'main = "rag-assistant-worker.mjs"' in toml and "ORIGIN_API_KEY" not in toml  # the key is a secret
    assert os.path.exists(ROOT / "tests" / "test_proxy_worker.mjs")


# --------------------------------------------------------------------------- found in live end-to-end runs
def test_text_written_tool_calls_are_executed_not_shown(store, retriever):
    from app.agent.tools import ToolBox
    from app.llm.providers import OpenAICompatLLM, text_tool_calls

    class R:
        status_code = 200

        def __init__(self, msg):
            self.msg = msg

        def json(self):
            return {"choices": [{"message": self.msg}]}

    class H:
        def __init__(self):
            self.replies = [
                R({"content": '<tool_call>\n{{"name": "search_docs", "arguments": {"query": "baseline", "k": 1}}}\n</tool_call>'}),
                R({"content": "Baseline means expected units [guide.md]."}),
            ]

        def post(self, url, headers=None, json=None, timeout=None, stream=False):
            return self.replies.pop(0)

    tb = ToolBox(store, retriever)
    out = OpenAICompatLLM(None, "http://127.0.0.1:1/v1", "qwen", http=H()).converse("s", [], "baseline?", tb, 3)
    assert out == "Baseline means expected units [guide.md]." and tb.calls[0]["tool"] == "search_docs"
    assert text_tool_calls('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>', {"search_docs"}) == []


def test_wide_tables_list_every_column_quoted_as_duckdb_needs():
    s = DataStore(":memory:")
    s.load_dataframe("wide", pd.DataFrame([{f"c{i}": i for i in range(45)} | {"ISO4217-code": "AED", "Name": "x"}]))
    summary = s.schema_summary()
    assert '"ISO4217-code"' in summary and '"Name"' in summary and "c44" in summary
    s.close()


def test_record_retention_questions_route_to_documents():
    from app.llm.intent import IntentRouter

    plan = IntentRouter(model_fallback=False).plan("How long should a business keep employment tax records?")
    assert plan.intent == "documents" and plan.method == "rules"
