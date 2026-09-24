"""Bank matching, line items, aging, reports (incl. document-vs-spreadsheet cross-checks) and HTML safety."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.accounting import analytics, bank, qbo_sync, reports
from app.data import ingest
from app.data.store import DataStore
from app.integrations.quickbooks import MockQuickBooks
from app.invoices.extract import extract_invoice, extract_line_items

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "data" / "qbo_sandbox" / "sandbox_company.json"


@pytest.fixture
def books(tmp_path):
    store = DataStore(str(tmp_path / "b.duckdb"))
    ingest.load_tables(store, str(ROOT / "data" / "sample"))
    qbo_sync.sync(MockQuickBooks(FIXTURE), store, today=date(2026, 7, 15))
    yield store
    store.close()


def test_bank_matching_on_sample_statement(books):
    rows = bank.reconcile_bank(books)
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    assert {r["qbo_doc_number"] for r in by["bill_payment"]} == {"INV-10421", "AFC-3302"}
    assert {r["qbo_doc_number"] for r in by["customer_receipt"]} == {"1001", "1003", "1004"}
    assert {r["description"] for r in by["no_bill"]} == {"Payroll", "Utilities - Easton Power & Water", "Bank fee"}
    # Harbor Waste's bill is marked paid in QuickBooks but no payment is on the statement
    assert [r["counterparty"] for r in by["paid_without_bank_evidence"]] == ["Harbor Waste Services"]
    bank.load_bank_reconciliation(books, rows)
    assert "bank_reconciliation" in books.tables()


def test_bank_columns_are_flexible(tmp_path):
    store = DataStore(str(tmp_path / "f.duckdb"))
    store.load_dataframe("bank_transactions", pd.DataFrame(
        [{"Posted": "2026-01-02", "Details": "ACME LTD INV-77", "Amount": -120.0}]))
    store.load_dataframe("qbo_bills", pd.DataFrame([{"id": "1", "doc_number": "INV-77", "vendor_name": "Acme Ltd",
                                                     "txn_date": "2026-01-01", "total": 120.0, "balance": 0.0}]))
    rows = bank.reconcile_bank(store)
    assert rows[0]["status"] == "bill_payment"
    store.close()


def test_line_items_and_sum_checks():
    text = ("Acme Tools LLC\nINVOICE\nInvoice No: A-9\nDate: 2026-01-05\n"
            "Description          Qty   Unit price   Amount\n"
            "Widget, large         2       10.00       20.00\n"
            "Gadget                3        5.00       16.00\n"
            "Subtotal: $40.00\nTax: $0.00\nTotal: $40.00\n")
    ex = extract_invoice(text)
    assert [(i["description"], i["quantity"], i["amount"]) for i in ex.line_items] == [
        ("Widget, large", 2.0, 20.0), ("Gadget", 3.0, 16.0)]
    assert any("3 x 5.00 = 15.00" in i for i in ex.issues)
    assert any("Line items add up to 36.00, but the subtotal is 40.00" in i for i in ex.issues)
    assert extract_line_items(["no table here", "Total 5.00"]) == []


def test_aging_buckets_and_as_of(books):
    ap = analytics.aging(books, "qbo_bills", date(2026, 7, 15))
    assert [b["bucket"] for b in ap] == analytics.AGING_BUCKETS
    assert round(sum(b["amount"] for b in ap), 2) == 20663.35
    assert ap[0]["amount"] == 3105.75  # Northgate not yet due on 15 July
    later = analytics.aging(books, "qbo_bills", date(2026, 12, 31))
    assert later[0]["amount"] == 0 and later[-1]["amount"] > 0
    assert analytics.as_of_date("2026-07-15") == date(2026, 7, 15)
    assert analytics.as_of_date("garbage") == date.today()


def test_kpis_and_budget(books):
    tiles = {t["id"]: t for t in analytics.kpis(books, date(2026, 7, 15))}
    assert tiles["ap"]["value"] == 20663.35 and tiles["cash"]["value"] == 35421.35
    budget = analytics.budget_vs_actual(books)
    assert any(r["item"] == "Car park resurfacing" and r["committed"] > r["budget"] for r in budget)


def _ctx(books, docs=None):
    from app.approvals import ApprovalQueue

    if docs is None:
        docs = ingest.load_documents(str(ROOT / "data" / "sample" / "documents"))
    return reports.ReportContext(books, docs, ApprovalQueue(None), date(2026, 7, 15))


def test_project_report_quotes_sources_and_flags_discrepancy(books):
    md = reports.report_project(_ctx(books))
    assert "Over budget:** Car park resurfacing" in md
    assert "states spent of $118,650.00, but the budget spreadsheet totals $88,590.00" in md
    assert "Fire door inspection certificate - OVERDUE since 1 June 2026. **[OVERDUE]**" in md
    assert "_Source: Project_Status_Riverside_Renovation.pdf_" in md
    assert "Target practical completion: 29 August 2026" in md  # schedule section, not the title


def test_outstanding_and_aging_reports(books):
    out = reports.report_outstanding(_ctx(books))
    assert "Electrical compliance sign-off" in out and "| OVERDUE |" in out
    assert "Juniper Yoga Studio | 1003 | 45" in out
    aging = reports.report_aging(_ctx(books))
    assert "Greenleaf Grounds Care Co. | GL-2291 | 2026-06-11 | 34" in aging
    assert "(DRAFT)" in aging


def test_report_html_escapes_content():
    html = reports.markdown_to_html("# T\n\n| a | b |\n|---|---|\n| <script>x</script> | **ok** |\n\n- <img src=x>", "T")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "<strong>ok</strong>" in html and "&lt;img src=x&gt;" in html


def test_doc_sections_parsing():
    secs = reports.doc_sections("Title Report\nIntro text.\nOutstanding tasks\n1. First - due 1 May 2026.\ncontinued\n2. Second\nRisks\nSome risk.")
    assert reports.numbered_items(secs["Outstanding tasks"]) == ["First - due 1 May 2026. continued", "Second"]
    assert reports._task_flag("First - due 1 May 2026.", date(2026, 7, 1)) == "OVERDUE"
