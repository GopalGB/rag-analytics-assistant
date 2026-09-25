"""Invoice extraction: accuracy on the synthetic set, uncertainty flags, grounded AI assist, review state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.documents.ocr import OCREngine
from app.documents.parsers import ParsedDocument, parse_file
from app.invoices.extract import extract_invoice, looks_like_invoice, parse_date
from app.invoices.registry import InvoiceRegistry

ROOT = Path(__file__).resolve().parent.parent
TRUTH = {t["file"]: t for t in json.loads((ROOT / "data" / "ground_truth" / "invoices.json").read_text())}
HAS_OCR = shutil.which("tesseract") is not None
FIELDS = ["supplier", "invoice_number", "invoice_date", "due_date", "subtotal", "tax", "total"]


def _path(name: str) -> Path:
    p = ROOT / "data" / "sample" / "invoices" / name
    return p if p.exists() else ROOT / "data" / "unseen_invoices" / name


@pytest.mark.parametrize("name", sorted(TRUTH))
def test_extraction_matches_ground_truth(name):
    if (name.endswith(".png") or "scanned" in name) and not HAS_OCR:
        pytest.skip("tesseract not installed")
    doc = parse_file(_path(name), name, OCREngine())
    ex = extract_invoice(doc.text, ocr=doc.used_ocr, warnings=doc.warnings)
    truth = TRUTH[name]
    wrong = {f: (ex.value(f), truth[f]) for f in FIELDS if ex.value(f) != truth[f]}
    assert not wrong, wrong
    assert looks_like_invoice(doc.text)


def test_ambiguous_dates_are_flagged():
    d = parse_date("05/12/2026", "MDY")
    assert d.iso == "2026-05-12" and d.alternative == "2026-12-05"
    assert parse_date("05/12/2026", "DMY").iso == "2026-12-05"
    assert parse_date("21-06-2026").alternative is None  # 21 can't be a month
    assert parse_date("18 May 2026").iso == "2026-05-18"
    assert parse_date("June 25, 2026").iso == "2026-06-25"
    assert parse_date("31/02/2026") is None
    doc = parse_file(_path("greenleaf_GL-2291.pdf"), "g.pdf", OCREngine())
    ex = extract_invoice(doc.text)
    assert any("ambiguous" in i for i in ex.issues)
    assert ex.fields["invoice_date"].confidence < 0.6


def test_missing_and_inconsistent_values_are_flagged():
    ex = extract_invoice("ACME TOOLS LLC\nINVOICE\nDate: 2026-01-05\nSubtotal: $100.00\nTax: $8.00\nTotal: $118.00\n")
    assert ex.value("invoice_number") is None
    assert any("Missing invoice number" in i for i in ex.issues)
    assert any("Totals don't add up" in i for i in ex.issues)
    assert ex.confidence == 0.0


def test_not_an_invoice():
    assert not looks_like_invoice("Supply Agreement\nInvoices are payable within 30 days.")


class _FakeExtractorLLM:
    name, is_local = "fake", True

    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, system, prompt):
        return "Here you go:\n" + json.dumps(self.payload)


def test_ai_values_must_be_on_the_document():
    text = (ROOT / "data" / "sample" / "documents" / "Expense_and_Invoice_Approval_Policy.md").read_text()
    text = "Metro Office Solutions Inc\nINVOICE\nInvoice No: MOS-1\nDate: 2026-05-15\nTotal: $689.42\n" + text
    llm = _FakeExtractorLLM({"invoice_number": "MOS-999", "total": 689.42, "po_number": "PO-777"})
    ex = extract_invoice(text, llm=llm)
    assert ex.method == "rules+ai"
    assert ex.value("invoice_number") == "MOS-1"  # hallucinated MOS-999 rejected
    assert ex.value("po_number") is None
    assert any("does not appear on the document" in i for i in ex.issues)
    assert ex.fields["total"].confidence >= 0.95  # rules and AI agree


def test_ai_placeholders_count_as_missing():
    text = "Blue Sky Services\nINVOICE\nDate: 2026-02-01\nTotal: $50.00\n"
    ex = extract_invoice(text, llm=_FakeExtractorLLM({"invoice_number": "Not provided on invoice", "po_number": "N/A"}))
    assert ex.value("invoice_number") is None
    assert not any("AI suggested" in i for i in ex.issues)
    assert any("Missing invoice number" in i for i in ex.issues)


def test_ai_fills_gap_only_with_grounded_value():
    text = "Blue Sky Services\nINVOICE\nRef MX-42 issued 2026-02-01\nTotal: $50.00\n"
    ex = extract_invoice(text, llm=_FakeExtractorLLM({"invoice_number": "MX-42"}))
    assert ex.value("invoice_number") == "MX-42"
    assert ex.fields["invoice_number"].note.startswith("found by the AI model only")


def test_ai_disagreement_is_surfaced():
    text = "Blue Sky Services\nINVOICE\nInvoice No: BS-1\nDate: 2026-02-01\nNet amount 40.00\nTotal: $50.00\n"
    ex = extract_invoice(text, llm=_FakeExtractorLLM({"total": 40.00}))
    assert ex.value("total") == 50.0
    assert any("disagree" in i for i in ex.issues)


def test_llm_failure_falls_back_to_rules():
    class Down:
        def complete(self, s, p):
            raise ConnectionError

    ex = extract_invoice("X Co\nINVOICE\nInvoice No: 1\nDate: 2026-01-01\nTotal: 5.00\n", llm=Down())
    assert ex.value("total") == 5.0 and any("could not be reached" in i for i in ex.issues)


def _doc(file: str, text: str, sha: str) -> ParsedDocument:
    return ParsedDocument(file=file, sha256=sha, kind="text", pages=[text])


def test_registry_duplicates_reviews_and_table(tmp_path: Path):
    text = "Acme Supply Co\nINVOICE\nInvoice No: A-1\nDate: 2026-03-01\nSubtotal: 10.00\nTax: 0.00\nTotal: $10.00\n"
    docs = [
        _doc("invoices/a.txt", text, "a" * 64),
        _doc("invoices/a_copy.txt", text + "\nCOPY", "b" * 64),
        _doc("documents/contract.txt", "INVOICE\nTotal: 5.00", "c" * 64),  # documents/ folder: not an invoice
    ]
    reg = InvoiceRegistry(tmp_path / "reviews.json")
    recs = reg.build(docs, known_suppliers=["ACME Supply Company"])
    assert len(recs) == 2
    assert recs[0].values["supplier"] == "ACME Supply Company"  # canonicalised to the known vendor
    assert recs[1].duplicate_of == "invoices/a.txt"
    with pytest.raises(ValueError):
        reg.review(recs[0].id, "approved", reviewer="  ")
    with pytest.raises(ValueError):
        reg.review(recs[0].id, "approved", reviewer="Sam", corrections={"invoice_date": "not a date"})
    reg.review(recs[0].id, "approved", reviewer="Sam", corrections={"total": "12.50"})
    # reviews survive a restart and a rebuild
    reg2 = InvoiceRegistry(tmp_path / "reviews.json")
    rec = reg2.build(docs)[0]
    assert rec.status == "approved" and rec.values["total"] == 12.5 and rec.reviewed_by == "Sam"
    df = reg2.dataframe()
    assert list(df["status"]) == ["approved", "needs_review"]
    assert df["issue_count"].iloc[1] >= 1
