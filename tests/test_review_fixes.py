"""Regression tests for review findings: bank-table detection, failed spreadsheet loads, privacy of the
intent classifier and of multi-step tool use, CTE shadowing, the final Anthropic call, CSRF/Host checks,
Unicode reviewer names, local model hosts, and safe backups/restores."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.accounting import bank, qbo_sync
from app.agent.engine import AgentEngine
from app.agent.memory import ConversationMemory
from app.agent.tools import ToolBox
from app.config import Settings
from app.data import ingest, watcher
from app.data.store import DataStore
from app.integrations.quickbooks import MockQuickBooks
from app.llm.intent import IntentRouter
from app.llm.privacy import PrivacyGuard, PrivacyPolicy, PrivacyRouter
from app.llm.providers import AnthropicLLM, is_local_url
from app.llm.router import ModelRouter
from app.middleware import install_security_middleware
from app.security import InputGuard

ROOT = Path(__file__).resolve().parent.parent


def _policy(**kw):
    return PrivacyPolicy(**{"allow_cloud": True, "cloud_allowed": frozenset({"documents"}), "redact_pii": True, **kw})


# --------------------------------------------------------------------------- bank tables
def test_reconciliation_output_is_not_read_back_as_a_bank_statement(tmp_path):
    store = DataStore(str(tmp_path / "b.duckdb"))
    ingest.load_tables(store, str(ROOT / "data" / "sample"))
    qbo_sync.sync(MockQuickBooks(ROOT / "data" / "qbo_sandbox" / "sandbox_company.json"), store, today=date(2026, 7, 15))
    first = bank.reconcile_bank(store)
    bank.load_bank_reconciliation(store, first)
    assert "bank_reconciliation" not in bank.bank_tables(store)
    second = bank.reconcile_bank(store)
    assert len(second) == len(first)
    assert ingest._safe_table_name("bank_reconciliation") == "file_bank_reconciliation"
    store.close()


# --------------------------------------------------------------------------- failed loads
def test_failed_spreadsheet_load_keeps_previous_table(store, data_dir, monkeypatch, retriever):
    assert "sales" in store.file_tables
    monkeypatch.setattr(ingest, "_read_table", lambda path: (_ for _ in ()).throw(OSError("file is mid-copy")))
    engine = AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=None, memory=ConversationMemory())
    watcher.reindex(engine, str(data_dir))
    assert "sales" in store.tables() and "sales" in store.file_tables
    (data_dir / "sales.csv").unlink()  # a file that is really gone is still dropped
    watcher.reindex(engine, str(data_dir))
    assert "sales" not in store.tables()


# --------------------------------------------------------------------------- privacy
class CloudClassifier:
    name, is_local, provider, model = "openai:mini", False, "openai", "mini"

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, system, prompt):
        self.prompts.append(prompt)
        return json.dumps({"intent": "general", "confidence": 0.9, "reason": "chat"})


def test_intent_classifier_masks_pii_before_a_cloud_model(store, retriever):
    model = CloudClassifier()
    engine = AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=None, memory=ConversationMemory(),
                         router=ModelRouter({"fast": [model]}), intents=IntentRouter(),
                         privacy=PrivacyRouter(_policy()))
    q = "hmm, anything odd lately? reply to dana@example.com"
    engine._classifier(q)(q)
    assert model.prompts and "dana@example.com" not in model.prompts[0] and "[EMAIL]" in model.prompts[0]


def test_toolbox_records_every_class_it_reads(store, retriever):
    store.load_dataframe("qbo_bills", pd.DataFrame([{"id": "1", "total": 5.0}]))
    tb = ToolBox(store, retriever, privacy=PrivacyGuard(_policy()))  # local model: nothing blocked
    tb.run("run_sql", {"sql": "SELECT * FROM qbo_bills"})
    tb.run("run_sql", {"sql": "SELECT * FROM sales"})  # the last query alone would look harmless
    assert tb.last_sql == "SELECT * FROM sales"
    assert "accounting" in tb.touched_classes

    class Hit:
        file, page, chunk_id, score, text = "invoices/summit.pdf", 1, 0, 1.0, "Total 4,871.25"

    tb.add_source(Hit())
    assert "invoices" in tb.touched_classes


def test_local_turn_that_read_accounting_data_is_kept_from_cloud(store, retriever):
    store.load_dataframe("qbo_bills", pd.DataFrame([{"id": "1", "total": 5.0}]))

    class Local:
        name, is_local, provider, model = "ollama:qwen", True, "ollama", "qwen"

        def converse(self, system, history, question, toolbox, max_iters):
            toolbox.run("run_sql", {"sql": "SELECT * FROM qbo_bills"})
            toolbox.run("run_sql", {"sql": "SELECT * FROM sales"})
            return "Total spend is 5.0."

    engine = AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=None, memory=ConversationMemory(),
                         router=ModelRouter({"fast": [Local()], "strong": [Local()]}),
                         intents=IntentRouter(model_fallback=False), privacy=PrivacyRouter(_policy()))
    out = engine.answer("s", "Summarise sales by region")
    assert out["routing"]["privacy"]["sensitive"] is True


def test_cte_named_like_a_real_table_counts_as_that_table(store, retriever):
    store.load_dataframe("qbo_bills", pd.DataFrame([{"id": "1", "total": 5.0}]))
    sql = "WITH qbo_bills AS (SELECT * FROM qbo_bills) SELECT * FROM qbo_bills"
    assert "qbo_bills" in store.referenced_tables(sql)
    assert store.referenced_tables("WITH x AS (SELECT * FROM sales) SELECT * FROM x") == {"sales"}
    guard = PrivacyGuard(_policy())
    guard.cloud = True
    assert "blocked by privacy policy" in ToolBox(store, retriever, privacy=guard).run("run_sql", {"sql": sql})["error"]


# --------------------------------------------------------------------------- final Anthropic call
class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _HTTP:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.bodies.append(__import__("copy").deepcopy(json))
        return _Resp(self.replies.pop(0))


def test_anthropic_final_answer_keeps_tools_and_alternates_roles(store, retriever):
    tool_turn = {"content": [{"type": "tool_use", "id": "t1", "name": "search_docs", "input": {"query": "baseline"}}]}
    http = _HTTP([tool_turn, {"content": [{"type": "text", "text": "Baseline is expected units."}]}])
    out = AnthropicLLM("k", "claude", http=http).converse("s", [], "baseline?", ToolBox(store, retriever), 1)
    assert out == "Baseline is expected units."
    final = http.bodies[-1]
    assert final["tools"] and final["tool_choice"] == {"type": "none"}
    roles = [m["role"] for m in final["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))
    assert final["messages"][-1]["content"][-1]["type"] == "text"


# --------------------------------------------------------------------------- HTTP: CSRF, Host, X-User
def _app(**settings):
    app = FastAPI()

    @app.post("/refresh")
    def refresh() -> dict:
        return {"ok": True}

    install_security_middleware(app, Settings(**{"allowed_hosts": "127.0.0.1,localhost,testserver", **settings}))
    return TestClient(app)


def test_cross_site_posts_are_rejected():
    c = _app()
    assert c.post("/refresh").status_code == 200  # scripts / curl send no Origin
    assert c.post("/refresh", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/refresh", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/refresh", headers={"Origin": "null"}).status_code == 403
    assert c.post("/refresh", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/refresh", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_unknown_host_is_rejected():
    c = _app()
    assert c.post("/refresh", headers={"Host": "rebind.evil.example"}).status_code == 400
    assert _app(allowed_hosts="*").post("/refresh", headers={"Host": "anything.example"}).status_code == 200


def test_unicode_reviewer_name_round_trips():
    from app.main import _actor

    scope = {"type": "http", "headers": [(b"x-user", quote("王芳 Андрей").encode())]}
    assert _actor(Request(scope)) == "王芳 Андрей"


def test_only_declared_service_names_are_local():
    from app.llm.providers import set_trusted_local_hosts

    assert is_local_url("http://host.docker.internal:11434/v1")
    assert not is_local_url("http://ollama:11434/v1")  # a bare name could resolve anywhere
    assert not is_local_url("http://model-gateway/v1")
    set_trusted_local_hosts("ollama")
    try:
        assert is_local_url("http://ollama:11434/v1") and not is_local_url("http://model-gateway/v1")
    finally:
        set_trusted_local_hosts("")
    assert not is_local_url("https://api.groq.com/openai/v1")


# --------------------------------------------------------------------------- backups
def _backup_module():
    spec = importlib.util.spec_from_file_location("backup_script", ROOT / "scripts" / "backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backup_keeps_data_cache_folders_and_skips_storage_cache(tmp_path):
    mod = _backup_module()
    data, storage = tmp_path / "data", tmp_path / "storage"
    (data / "cache").mkdir(parents=True)
    (data / "cache" / "policy.md").write_text("source document")
    (storage / "cache").mkdir(parents=True)
    (storage / "cache" / "parsed.json").write_text("{}")
    (storage / "approvals.json").write_text("[]")
    archive = mod.create(tmp_path / "out", False, True, Settings(data_dir=str(data), storage_dir=str(storage)))
    names = set(mod.verify(archive)["files"])
    assert "data/cache/policy.md" in names and "storage/approvals.json" in names
    assert "storage/cache/parsed.json" not in names


def _archive(path: Path, files: dict[str, bytes], listed: dict[str, bytes]) -> Path:
    import hashlib

    manifest = {"created": "now", "app_version": "t", "files": {
        n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for n, b in listed.items()}}
    with tarfile.open(path, "w:gz") as tar:
        for name, body in {"manifest.json": json.dumps(manifest).encode(), **files}.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def test_restore_rejects_unlisted_members(tmp_path):
    mod = _backup_module()
    good = {"storage/approvals.json": b"[]"}
    archive = _archive(tmp_path / "a.tar.gz", {**good, ".env": b"APP_API_KEY=attacker"}, good)
    with pytest.raises(SystemExit, match="not listed"):
        mod.verify(archive)


def test_restore_refuses_to_overwrite_documents_without_force(tmp_path):
    mod = _backup_module()
    data, storage = tmp_path / "data", tmp_path / "storage"
    data.mkdir()
    (data / "lease.docx").write_bytes(b"current")
    files = {"data/lease.docx": b"old", "storage/approvals.json": b"[]"}
    archive = _archive(tmp_path / "b.tar.gz", files, files)
    settings = Settings(data_dir=str(data), storage_dir=str(storage))
    with pytest.raises(SystemExit, match="already exist"):
        mod.restore(archive, False, settings)
    assert (data / "lease.docx").read_bytes() == b"current" and not storage.exists()
    mod.restore(archive, True, settings)
    assert (data / "lease.docx").read_bytes() == b"old"


# --------------------------------------------------------------------------- second review round
def test_spreadsheets_never_share_a_table(tmp_path):
    root = tmp_path / "data"
    (root / "2025").mkdir(parents=True)
    (root / "2026").mkdir()
    (root / "2025" / "budget.csv").write_text("a\n1\n")
    (root / "2026" / "budget.csv").write_text("a\n2\n")
    (root / "invoice_lines.csv").write_text("a\n3\n")
    (root / "file_invoice_lines.csv").write_text("a\n4\n")
    store = DataStore(str(tmp_path / "t.duckdb"))
    loaded = ingest.load_tables(store, str(root))
    assert len(loaded) == 4 and len(store.file_tables) == 4
    values = sorted(store.run_select(f'SELECT a FROM "{t}"')[1][0][0] for t in loaded)
    assert values == [1, 2, 3, 4]
    store.close()


def test_watcher_retries_when_a_spreadsheet_could_not_be_read(tmp_path):
    import asyncio

    (tmp_path / "x.csv").write_text("a\n1\n")
    calls = []

    def reindex():
        calls.append(1)
        if len(calls) == 1:
            (tmp_path / "y.csv").write_text("a\n2\n")  # changes the folder once
            return {"failed_tables": ["y"]}
        return {"failed_tables": []}

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(watcher.run_watcher(reindex, str(tmp_path), 0, stop))
        await asyncio.sleep(0.05)  # let the watcher take its first snapshot
        (tmp_path / "z.csv").write_text("a\n3\n")
        for _ in range(1000):  # up to 10 s on a busy machine; normally a few ticks
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        stop.set()
        await task

    asyncio.run(run())
    assert len(calls) >= 2  # retried without any further change to the folder


def test_bedrock_tool_call_after_budget_is_an_error(store, retriever):
    from app.llm.providers import BedrockLLM, LLMError

    llm = BedrockLLM.__new__(BedrockLLM)
    replies = iter([{"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": f"t{i}", "name": "search_docs", "input": {"query": "x"}}}]}}} for i in range(3)])
    llm._converse = lambda **kw: next(replies)
    llm._tool_config = lambda tb: {"tools": [{"toolSpec": {"name": "search_docs"}}]}
    with pytest.raises(LLMError, match="tool budget"):
        llm._converse_all("s", [], "q", ToolBox(store, retriever), 2)


def test_unreadable_audit_line_is_reported_not_fatal(tmp_path):
    from app.audit import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("a")
    log.record("b")
    with path.open("a") as fh:
        fh.write('{"ts": "truncated')  # a crash mid-write
    reopened = AuditLog(path)  # must not raise
    assert reopened.verify() == {"ok": False, "entries": 3, "broken_at": 3}
    assert [e["event"] for e in reopened.tail(5)] == ["a", "b"]


def test_failed_queries_are_reported_not_shown_as_zero(tmp_path):
    from app.accounting import analytics

    store = DataStore(str(tmp_path / "q.duckdb"))
    store.load_dataframe("qbo_bills", pd.DataFrame([{"id": "1", "total": 5.0}]))  # no due_date/balance columns
    with analytics.collect_query_errors() as errors:
        rows = analytics.aging(store, "qbo_bills", date(2026, 7, 15))
    assert errors and "balance" in errors[0].lower()
    assert all(b["amount"] == 0 for b in rows)  # the page shows the warning next to these zeros
    assert analytics.query(store, "SELECT nope FROM qbo_bills") == []  # outside a collector: logged only
    store.close()
