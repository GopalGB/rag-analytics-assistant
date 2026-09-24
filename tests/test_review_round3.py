"""Regression tests for the third review round: row caps, currencies, per-source privacy, audit shape,
bounded watcher retries, invoice-number placeholders, attention routing, tool-call arguments and the live
evaluator's handling of the API key."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.accounting import analytics, insights
from app.agent.tools import ToolBox
from app.audit import AuditLog
from app.data import watcher
from app.data.store import USER_MAX_ROWS, DataStore
from app.invoices.extract import extract_rules
from app.llm.intent import IntentRouter
from app.llm.privacy import PrivacyGuard, PrivacyPolicy
from app.llm.providers import text_tool_calls

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- row caps
def test_app_queries_see_every_row_and_report_truncation(tmp_path):
    store = DataStore(str(tmp_path / "a.duckdb"))
    store.load_dataframe("qbo_bills", pd.DataFrame({"total": [1.0] * 450}))
    assert len(store.run_select("SELECT * FROM qbo_bills", max_rows=100000)[1]) == USER_MAX_ROWS  # model/user SQL
    assert len(store.run_select("SELECT * FROM qbo_bills", max_rows=100000, internal=True)[1]) == 450
    with analytics.collect_query_errors() as errors:
        assert len(analytics.query(store, "SELECT * FROM qbo_bills")) == 450 and errors == []
        analytics.query(store, "SELECT * FROM qbo_bills", max_rows=100)
    assert errors and "cut off at 100 rows" in errors[0]
    store.close()


# --------------------------------------------------------------------------- currencies
def _store_with_invoices(tmp_path) -> DataStore:
    store = DataStore(str(tmp_path / "c.duckdb"))
    store.load_dataframe("invoices", pd.DataFrame([
        {"file": "invoices/eu.pdf", "supplier": "Euro GmbH", "invoice_number": "E-1", "total": 7000.0,
         "currency": "EUR", "status": "needs_review", "issues": "", "issue_count": 0, "duplicate_of": None},
        {"file": "invoices/us.pdf", "supplier": "Acme", "invoice_number": "A-1", "total": 100.0,
         "currency": "USD", "status": "ok", "issues": "", "issue_count": 0, "duplicate_of": None},
    ]))
    store.load_dataframe("invoice_reconciliation", pd.DataFrame([
        {"file": "invoices/eu.pdf", "supplier": "Euro GmbH", "invoice_number": "E-1", "invoice_date": "2026-07-01",
         "document_total": 7000.0, "qbo_total": 6500.0, "difference": 500.0, "status": "amount_mismatch",
         "severity": "issue", "detail": "differs"},
        {"file": "invoices/us.pdf", "supplier": "Acme", "invoice_number": "A-1", "invoice_date": "2026-07-01",
         "document_total": 100.0, "qbo_total": 90.0, "difference": 10.0, "status": "amount_mismatch",
         "severity": "issue", "detail": "differs"},
    ]))
    return store


def test_quickbooks_only_items_keep_the_bill_currency(tmp_path):
    from app.accounting.reconcile import load_reconciliation, reconcile

    store = DataStore(str(tmp_path / "q.duckdb"))
    store.load_dataframe("qbo_bills", pd.DataFrame([
        {"id": "9", "doc_number": "GB-7", "vendor_name": "London Ltd", "txn_date": "2026-07-01", "due_date": None,
         "total": 900.0, "balance": 900.0, "currency": "GBP"}]))
    rows = reconcile([], store)
    assert rows[0]["status"] == "no_document" and rows[0]["currency"] == "GBP"
    load_reconciliation(store, rows)
    a = insights.attention(store, [], date(2026, 7, 15))
    item = next(i for i in a["items"] if "London Ltd" in i["title"] and i["category"] == "reconciliation")
    assert item["currency"] == "GBP"
    assert a["money_at_stake"] == 0 and a["money_at_stake_other"].get("GBP") == 900.0  # never in the USD total
    store.close()


def test_money_at_stake_is_never_summed_across_currencies(tmp_path):
    store = _store_with_invoices(tmp_path)
    policy = SimpleDoc("documents/policy.md", "- Invoices over $5,000: approved by a director.")
    a = insights.attention(store, [policy], date(2026, 7, 15))
    assert a["money_at_stake"] == 10.0 and a["money_at_stake_other"] == {"EUR": 500.0}
    eu = next(i for i in a["items"] if "Euro GmbH" in i["title"])
    assert eu["currency"] == "EUR"
    # a EUR total is not compared against the policy's dollar thresholds
    assert not any(i["category"] == "policy" and "Euro GmbH" in i["title"] for i in a["items"])
    assert insights.money(1234.5, "EUR") == "EUR 1,234.50" and insights.money(1234.5, None) == "$1,234.50"
    view = insights.for_model(a)
    assert view["money_at_stake_other"] == {"EUR": 500.0} and all("currency" in i for i in view["items"])
    store.close()


class SimpleDoc:
    def __init__(self, file: str, text: str):
        self.file, self.text, self.pages = file, text, [text]


# --------------------------------------------------------------------------- per-source privacy
def test_attention_tool_blocks_cloud_when_any_source_is_not_allowed(tmp_path, retriever):
    store = DataStore(str(tmp_path / "p.duckdb"))
    result = {"as_of": "2026-07-15", "counts": {}, "money_at_stake": 0, "money_at_stake_other": {}, "currency": "USD",
              "items": [{"severity": "critical", "category": "bank", "title": "x", "detail": "y", "amount": 1.0,
                         "currency": "USD", "source": {"type": "table", "name": "bank_reconciliation"}}]}
    guard = PrivacyGuard(PrivacyPolicy(True, frozenset({"documents", "accounting"}), True))
    guard.cloud = True
    tb = ToolBox(store, retriever, privacy=guard, insights=lambda: result)
    assert "blocked by privacy policy" in tb.run("attention_items", {})["error"]  # accounting ok, bank is not
    guard.policy = PrivacyPolicy(True, frozenset({"documents", "accounting", "bank"}), True)
    out = tb.run("attention_items", {})
    assert "items" in out and {"accounting", "bank"} <= tb.touched_classes
    store.close()


# --------------------------------------------------------------------------- audit log shape
@pytest.mark.parametrize("bad", ["{}", "null", "[1, 2]", '{"hash": 5}', "{not json"])
def test_wrong_shaped_audit_lines_are_a_broken_chain_not_a_crash(tmp_path, bad):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("one")
    with path.open("a") as fh:
        fh.write(bad + "\n")
    again = AuditLog(path)  # must start
    assert all(isinstance(e, dict) and "hash" in e for e in again.tail(10))
    assert again.verify() == {"ok": False, "entries": 2, "broken_at": 2}


# --------------------------------------------------------------------------- watcher retries are bounded
def test_watcher_stops_rebuilding_a_folder_that_keeps_failing(tmp_path):
    (tmp_path / "x.csv").write_text("a\n1\n")
    calls = []

    def reindex():
        calls.append(1)
        return {"failed_tables": ["broken"]}

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(watcher.run_watcher(reindex, str(tmp_path), 0, stop))
        await asyncio.sleep(0.05)
        (tmp_path / "broken.csv").write_text("\x00not a table")
        for _ in range(300):
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(run())
    assert len(calls) == watcher.MAX_FAILED_RETRIES  # then it waits for the folder to change again


# --------------------------------------------------------------------------- invoice-number placeholders
@pytest.mark.parametrize("value", ["N/A", "TBD", "None", "MISSING", "AB/CD"])
def test_placeholders_are_not_invoice_numbers(value):
    ex = extract_rules(f"Acme Supplies Ltd\nInvoice Number: {value}\nTotal: $10.00")
    assert ex.fields["invoice_number"].value is None


def test_digit_free_numbers_only_from_their_own_pattern():
    assert extract_rules("Acme Ltd\nINVOICE\nInvoice INV-ABC\nTotal: $1.00").fields["invoice_number"].value == "INV-ABC"
    assert extract_rules("Acme Ltd\nInvoice No: INV-1042\nTotal $5").fields["invoice_number"].value == "INV-1042"


# --------------------------------------------------------------------------- attention routing
@pytest.mark.parametrize("question", ["What is on the onboarding checklist?",
                                      "What are the priority tasks on the Riverside project?",
                                      "What does the policy say about month-end close?"])
def test_document_questions_are_not_sent_to_the_attention_list(question):
    plan = IntentRouter(model_fallback=False).plan(question)
    assert plan.intent == "documents"


@pytest.mark.parametrize("question", ["What needs my attention this week?", "Give me the month-end checklist",
                                      "What are our top priorities?", "Anything urgent?"])
def test_attention_questions_still_route_to_accounting(question):
    assert IntentRouter(model_fallback=False).plan(question).intent == "accounting"


# --------------------------------------------------------------------------- recovered tool calls
@pytest.mark.parametrize("args,expected", [('""', {}), ("null", {}), ("[1]", {}), ('"not json"', {}),
                                           ('"{\\"sql\\": \\"SELECT 1\\"}"', {"sql": "SELECT 1"}),
                                           ('{"sql": "SELECT 1"}', {"sql": "SELECT 1"})])
def test_recovered_tool_arguments_are_always_a_json_object(args, expected):
    calls = text_tool_calls(f'<tool_call>{{"name": "run_sql", "arguments": {args}}}</tool_call>', {"run_sql"})
    assert json.loads(calls[0]["function"]["arguments"]) == expected


# --------------------------------------------------------------------------- live evaluator
def test_live_evaluator_protects_the_api_key():
    ev = _load_script("evaluate_live")
    with pytest.raises(SystemExit):
        ev.urllib_http("http://assistant.example.com/", "k" * 30)  # key over plain HTTP to a remote host
    ev.urllib_http("http://127.0.0.1:8000/", "k" * 30)  # loopback is fine
    ev.urllib_http("https://assistant.example.com/", "k" * 30)
    handler = ev._SameOriginRedirects(("https", "assistant.example.com"))
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.net/x") is None
    assert ev._check_answer("not_found", None, {"text": "Our auditor is Smith & Co.", "sources": []})
    assert ev._check_answer("not_found", None, {"text": "I couldn't find this in the documents.", "sources": []}) is None


def test_preflight_requires_storage_inside_tmp(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.isupper() or k in ("PATH", "HOME", "LANG")}
    env.update({"PUBLIC_DEMO": "true", "STORAGE_DIR": "/tmp-other/store", "PREFLIGHT_NO_DOTENV": "1"})
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "preflight.py"), "--target", "public-demo"],
                         capture_output=True, text=True, env=env, cwd=tmp_path)
    assert "[FAIL] STORAGE_DIR is under /tmp" in out.stdout, out.stdout
