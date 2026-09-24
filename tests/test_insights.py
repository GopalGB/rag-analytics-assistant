"""The "needs attention" list, deadlines read from documents, policy-aware approval checks and anomalies."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.accounting import insights, qbo_sync
from app.agent.tools import ToolBox
from app.data import ingest
from app.data.store import DataStore
from app.documents.deadlines import find_deadlines
from app.integrations.quickbooks import MockQuickBooks
from app.llm.privacy import PrivacyGuard, PrivacyPolicy

ROOT = Path(__file__).resolve().parent.parent
AS_OF = date(2026, 7, 15)


def _doc(file: str, text: str):
    return SimpleNamespace(file=file, text=text, pages=[text])


# --------------------------------------------------------------------------- deadlines
def test_deadlines_need_a_deadline_word_and_survive_line_wraps():
    doc = _doc("documents/project.pdf", "Schedule\nTarget practical completion: 29 August 2026.\n\n"
                                        "1. Electrical sign-off - due 30 June\n2026.\n\n"
                                        "Report date: 15 June 2026.\nNotice by 05/06/2026.\nSigned on 1 March 2019 (renewal).")
    got = {(d.date, d.kind, d.status) for d in find_deadlines([doc], AS_OF)}
    assert ("2026-08-29", "completion", "upcoming") in got
    assert ("2026-06-30", "inspection", "overdue") in got  # "due 30 June\n2026" re-joined
    assert not any(d == "2026-06-15" for d, _, _ in got)  # a report date is not a deadline
    assert not any(d.startswith("2019") for d, _, _ in got)  # old history is not something to act on
    assert all("05/06" not in d for d, _, _ in got)  # ambiguous numeric dates are skipped
    text = [d.text for d in find_deadlines([doc], AS_OF)]
    assert "Target practical completion: 29 August 2026." in text  # heading kept separate


def test_deadlines_on_the_sample_documents():
    docs = ingest.load_documents(str(ROOT / "data" / "sample"))
    found = {(d.date, d.status, d.file.rsplit("/", 1)[-1]) for d in find_deadlines(docs, AS_OF)}
    assert ("2026-06-01", "overdue", "Project_Status_Riverside_Renovation.pdf") in found  # fire door certificate
    assert ("2028-12-31", "later", "Office_Lease_Summary.docx") in found  # renewal notice
    assert ("2029-06-30", "later", "Office_Lease_Summary.docx") in found  # lease expiry
    assert not any("invoices/" in f for _, _, f in found)


# --------------------------------------------------------------------------- policy read from documents
POLICY = _doc("documents/policy.md", "## Approval thresholds\n- Invoices under $1,000: approved by the manager.\n"
                                     "- Invoices from $1,000 up to $5,000: approved by the finance lead.\n"
                                     "- Invoices over $5,000: approved by a director, and two quotes must be on file.\n"
                                     "All approved supplier invoices must be recorded as bills within 5 business days of receipt.")


def test_approval_rules_and_recording_rule_are_read_from_the_policy():
    rules = insights.approval_rules([POLICY])
    assert [(r.low, r.high) for r in rules] == [(0.0, 1000.0), (1000.0, 5000.0), (5000.0, float("inf"))]
    assert rules[2].approver.startswith("a director") and rules[2].file == "documents/policy.md"
    assert insights.recording_deadline_days([POLICY]) == (5, "documents/policy.md")


# --------------------------------------------------------------------------- the list on the sample company
@pytest.fixture(scope="module")
def sample():
    import os

    from app.config import Settings
    from app.workspace import Workspace

    tmp = Path(__import__("tempfile").mkdtemp())
    keep = {k: os.environ.get(k) for k in ("STORAGE_DIR", "DB_PATH", "LLM_PROVIDER", "EMBEDDING_PROVIDER", "AUTO_REINDEX",
                                           "REPORT_AS_OF", "DATA_DIR")}
    os.environ.update(STORAGE_DIR=str(tmp), DB_PATH=str(tmp / "a.duckdb"), LLM_PROVIDER="none", EMBEDDING_PROVIDER="local",
                      AUTO_REINDEX="false", REPORT_AS_OF="2026-07-15", DATA_DIR=str(ROOT / "data" / "sample"))
    try:
        ws = Workspace(Settings())
        ws.startup()
        yield ws
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_attention_ranks_real_problems_with_sources(sample):
    a = sample.attention()
    items = a["items"]
    titles = [i["title"] for i in items]
    assert len({i["id"] for i in items}) == len(items)
    assert [insights.SEVERITY_ORDER[i["severity"]] for i in items] == sorted(insights.SEVERITY_ORDER[i["severity"]] for i in items)
    critical = [i for i in items if i["severity"] == "critical"]
    assert any("Car park resurfacing is over budget by $6,400.00" in t for t in titles)
    assert any("INV-10421" in i["title"] and "duplicate" in i["title"] for i in critical)
    brightspark = next(i for i in critical if "BSC-118" in i["title"])
    assert "Policy: record within 5 business days" in brightspark["detail"]  # rule read from the policy file
    assert any("Harbor Waste" in i["title"] and "no payment on the bank statement" in i["title"] for i in critical)
    assert any("Fire door inspection certificate" in i["title"] for i in critical)
    director = next(i for i in items if i["category"] == "policy" and "PCW-0192" in i["title"])
    assert director["severity"] == "warning" and "director" in director["title"]
    assert director["source"]["name"].endswith("Expense_and_Invoice_Approval_Policy.md")
    assert all(i["source"].get("name") for i in items)
    assert a["money_at_stake"] > 0 and a["errors"] == []


def test_month_end_report(sample):
    md = sample.report("month_end")["markdown"]
    assert "# Month-end checklist (DRAFT)" in md and "## Do first" in md
    assert "- [ ] **Car park resurfacing is over budget" in md
    assert "## Deadlines found in the documents" in md and "Office_Lease_Summary.docx" in md
    assert "| over $5,000.00 | a director, and two quotes must be on file |" in md


# --------------------------------------------------------------------------- anomalies
def test_unusual_and_repeated_bills_are_flagged(tmp_path):
    store = DataStore(str(tmp_path / "a.duckdb"))
    store.load_dataframe("qbo_bills", pd.DataFrame([
        {"vendor_name": "Acme", "doc_number": "A1", "txn_date": "2026-05-01", "total": 100.0},
        {"vendor_name": "Acme", "doc_number": "A2", "txn_date": "2026-05-20", "total": 110.0},
        {"vendor_name": "Acme", "doc_number": "A3", "txn_date": "2026-06-02", "total": 950.0},
        {"vendor_name": "Beta", "doc_number": "B7", "txn_date": "2026-06-01", "total": 480.0},
        {"vendor_name": "Beta", "doc_number": "B9", "txn_date": "2026-06-20", "total": 480.0},
    ]))
    titles = [i["title"] for i in insights._anomalies(store, set(store.tables()))]
    assert "Acme bill A3 is unusually large" in titles
    assert "Beta billed $480.00 twice" in titles
    store.close()


# --------------------------------------------------------------------------- the agent tool
def test_attention_tool_for_local_models_and_blocked_for_cloud(tmp_path, retriever):
    store = DataStore(str(tmp_path / "t.duckdb"))
    ingest.load_tables(store, str(ROOT / "data" / "sample"))
    qbo_sync.sync(MockQuickBooks(ROOT / "data" / "qbo_sandbox" / "sandbox_company.json"), store, today=AS_OF)
    fn = lambda: insights.attention(store, [POLICY], AS_OF)  # noqa: E731
    assert "attention_items" not in ToolBox(store, retriever).allowed_tools  # only offered when wired
    guard = PrivacyGuard(PrivacyPolicy(True, frozenset({"documents"}), True))
    tb = ToolBox(store, retriever, privacy=guard, insights=fn)
    out = tb.run("attention_items", {"limit": 3})
    assert len(out["items"]) == 3 and "source" in out["items"][0] and "accounting" in tb.touched_classes
    guard.cloud = True
    assert "blocked by privacy policy" in tb.run("attention_items", {})["error"]
    store.close()


def test_prioritisation_questions_route_to_the_accounting_pipeline():
    from app.llm.intent import IntentRouter

    plan = IntentRouter(model_fallback=False).plan("What needs my attention this week?")
    assert plan.intent == "accounting" and "attention_items" in plan.tools


def test_no_model_answer_to_what_needs_attention(sample):
    r = sample.engine.answer("t", "What needs my attention this week?")
    assert r["route"] == "attention" and r["text"].startswith("No AI model is connected")
    assert "1. Do first: Car park resurfacing is over budget by $6,400.00. Committed" in r["text"]
    assert ".." not in r["text"] and any(s["file"].endswith("brightspark_BSC-118.pdf") for s in r["sources"])
