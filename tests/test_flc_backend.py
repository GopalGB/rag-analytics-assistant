"""Regression coverage for the synthetic FLC demo backend."""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.tools import ToolBox
from app.data import ingest


def test_invoice_parser_uses_grand_total_and_aed_before_or_after_amount():
    invoice = ingest.extract_invoice(
        """Supplier: Atlas Stationery
Invoice Number: AT-1042
Date: 2026-05-12
Subtotal: AED 99.00
Grand Total: AED 108.90
""",
        "atlas.pdf",
    )
    assert invoice["amount"] == "108.90"
    assert invoice["currency"] == "AED"
    assert invoice["missing_fields"] == []


def test_invoice_parser_marks_duplicate_and_invalid_fields_for_review():
    invoice = ingest.extract_invoice(
        """Supplier: Atlas
Supplier: Other Atlas
Invoice Number: AT-1042
Date: 2026-02-30
Amount Due: 12.5 AED
"""
    )
    assert invoice["supplier"] is None
    assert invoice["date"] is None
    assert {"supplier", "date"} <= set(invoice["review_fields"])
    assert "supplier" in invoice["evidence"]


def test_invoice_parser_never_conflates_payment_due_or_dollar_symbol_with_confirmed_fields():
    invoice = ingest.extract_invoice(
        """Supplier: Atlas
Invoice Number: AT-1042
Payment due: 2026-06-01
Amount: $12.50
"""
    )
    assert invoice["date"] is None
    assert invoice["currency"] is None
    assert {"date", "currency"} <= set(invoice["missing_fields"])
    assert "currency" in invoice["review_fields"]


def test_invoice_parser_marks_quantity_times_unit_price_mismatch_for_review():
    invoice = ingest.extract_invoice(
        """Supplier: Atlas
Invoice Number: AT-1042
Invoice Date: 2026-05-12
Currency: USD
Quantity: 3
Unit price: USD 10.00
Total: USD 25.00
"""
    )
    assert invoice["amount"] == "25.00"
    assert "amount" in invoice["review_fields"]


def test_invoice_parser_rejects_overprecise_money_and_literal_label_dots():
    invoice = ingest.extract_invoice("Invoice noX: wrong\nInvoice no.: INV-7\nAmount: USD 12.345")
    assert invoice["invoice_number"] == "INV-7"
    assert invoice["amount"] is None
    assert "amount" in invoice["review_fields"]


def test_source_registry_excludes_symlinks_and_read_document_rejects_them(tmp_path: Path):
    source = tmp_path / "source.md"
    source.write_text("safe", encoding="utf-8")
    (tmp_path / "linked.md").symlink_to(source)
    assert [path.name for path in ingest.source_paths(tmp_path)] == ["source.md"]
    try:
        ingest.read_document(tmp_path / "linked.md")
    except ingest.DocumentReadError:
        pass
    else:
        raise AssertionError("symlink must not be read")


def test_toolbox_rejects_malformed_tool_arguments(store, retriever):
    tools = ToolBox(store, retriever)
    assert "error" in tools.run("search_docs", {"query": "baseline", "k": "many"})
    assert "error" in tools.run("run_sql", {"sql": ["SELECT 1"]})


def test_toolbox_cites_retrieved_chunks_and_executed_csv_tables(store, retriever):
    tools = ToolBox(store, retriever)
    tools.run("search_docs", {"query": "baseline"})
    tools.run("run_sql", {"sql": "SELECT region, revenue FROM sales"})
    assert any(source["source_id"].startswith("guide.md:") and source["excerpt"] for source in tools.sources)
    assert any(source["source_id"] == "table:sales" and source["file"] == "sales.csv" for source in tools.sources)


def test_toolbox_returns_deterministic_invoice_records_with_per_file_citations(store, retriever):
    records = [
        {
            "source_file": "invoice_01.txt",
            "supplier": "Atlas",
            "invoice_number": "AT-1",
            "date": "2026-01-01",
            "amount": "100.00",
            "currency": "USD",
            "missing_fields": [],
            "review_fields": [],
            "evidence": {"amount": "USD 100.00"},
        }
    ]
    tools = ToolBox(store, retriever, invoice_records=records)
    payload = tools.run("invoice_records", {})
    assert payload == {
        "records": [
            {
                "source_file": "invoice_01.txt",
                "supplier": "Atlas",
                "invoice_number": "AT-1",
                "date": "2026-01-01",
                "amount": "100.00",
                "currency": "USD",
                "missing_fields": [],
                "review_fields": [],
            }
        ],
        "totals_by_currency": {"USD": "100.00"},
        "included_sources": {"USD": ["invoice_01.txt"]},
        "excluded_sources": [],
    }
    assert tools.sources == [
        {
            "source_id": "invoice:invoice_01.txt",
            "file": "invoice_01.txt",
            "excerpt": "Invoice extraction: supplier=Atlas; amount=100.00; currency=USD.",
            "text": "Invoice extraction: supplier=Atlas; amount=100.00; currency=USD.",
        }
    ]


def test_toolbox_aggregates_all_synthetic_invoices_without_truncation_or_unknown_currency(store, retriever):
    sample = Path(__file__).resolve().parents[1] / "data" / "sample"
    records = []
    for path in sorted(sample.glob("invoice_*")):
        parsed = ingest.extract_invoice(ingest.read_document(path), path.name)
        parsed["source_file"] = path.name
        records.append(parsed)
    records.append(
        {
            "source_file": "invoice_unknown.txt",
            "supplier": "Unknown Currency",
            "invoice_number": "UNKNOWN-1",
            "date": "2026-01-01",
            "amount": "9.99",
            "currency": "XYZ",
            "missing_fields": [],
            "review_fields": [],
        }
    )

    tools = ToolBox(store, retriever, invoice_records=records)
    payload = tools.run("invoice_records", {})

    assert len(payload["records"]) == 12
    assert len(json.dumps(payload, separators=(",", ":")).encode()) < 8_000
    assert payload["totals_by_currency"] == {"AED": "630.00", "USD": "2510.00"}
    assert payload["included_sources"] == {
        "AED": ["invoice_pdf_001.pdf", "invoice_pdf_002.pdf", "invoice_pdf_003.pdf"],
        "USD": ["invoice_01.txt", "invoice_02.txt", "invoice_03.txt", "invoice_04.txt", "invoice_05.txt"],
    }
    excluded = {item["source_file"]: item["reasons"] for item in payload["excluded_sources"]}
    assert "invoice_unknown.txt" in excluded
    assert any("unknown currency" in reason for reason in excluded["invoice_unknown.txt"])
    assert {source["file"] for source in tools.sources} == {record["source_file"] for record in records}


def test_qbo_adapter_only_uses_fixed_read_queries():
    from app.data.qbo import QBOSandboxClient

    calls: list[tuple[str, str, dict]] = []

    def fake_get(url: str, *, headers: dict, params: dict, timeout: int):
        calls.append((url, headers["Authorization"], params))

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"QueryResponse": {"Invoice": []}}

        return Response()

    client = QBOSandboxClient("token", "realm", transport=fake_get)
    assert client.list_invoices() == []
    assert client.list_vendors() == []
    assert calls[0][2]["query"] == "SELECT * FROM Invoice MAXRESULTS 50"
    assert calls[1][2]["query"] == "SELECT * FROM Vendor MAXRESULTS 50"
