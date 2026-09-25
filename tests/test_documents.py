"""Document parsing: text PDFs, scanned PDFs (OCR), Word, images, and honest failure when OCR is absent."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.data import ingest
from app.documents.ocr import OCREngine
from app.documents.parsers import ParseCache, parse_file

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample"
HAS_OCR = shutil.which("tesseract") is not None


def test_text_pdf_keeps_pages_and_layout():
    doc = parse_file(SAMPLE / "documents" / "Supplier_Agreement_Summit_Ridge_Electrical.pdf", "a.pdf", OCREngine())
    assert doc.kind == "pdf" and len(doc.pages) == 2
    assert "Net 30" in doc.pages[0] and "60 days written notice" in doc.pages[1]
    assert not doc.used_ocr and not doc.warnings


def test_docx_parsed_without_extra_dependencies():
    doc = parse_file(SAMPLE / "documents" / "Office_Lease_Summary.docx", "lease.docx", OCREngine())
    assert "Expiry date: 30 June 2029" in doc.text


def test_scanned_pdf_without_ocr_is_flagged_not_invented():
    doc = parse_file(SAMPLE / "invoices" / "pioneer_PCW-0192_scanned.pdf", "scan.pdf", OCREngine(enabled=False))
    assert doc.text.strip() == ""
    assert doc.warnings and "scanned" in doc.warnings[0]


@pytest.mark.skipif(not HAS_OCR, reason="tesseract not installed")
def test_scanned_pdf_with_local_ocr():
    doc = parse_file(SAMPLE / "invoices" / "pioneer_PCW-0192_scanned.pdf", "scan.pdf", OCREngine())
    assert doc.ocr_pages == [1]
    assert "PCW-0192" in doc.text and "12,990.00" in doc.text


def test_corrupt_file_does_not_break_indexing(tmp_path: Path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf")
    doc = parse_file(bad, "broken.pdf", OCREngine())
    assert doc.warnings


def test_parse_cache_reuses_results(tmp_path: Path):
    src = tmp_path / "note.md"
    src.write_text("hello cache", encoding="utf-8")
    cache = ParseCache(tmp_path / "cache")
    first = cache.parse(src, "note.md", OCREngine())
    assert list((tmp_path / "cache").glob("*.json"))
    again = ParseCache(tmp_path / "cache").parse(src, "renamed.md", OCREngine())
    assert again.text == first.text and again.file == "renamed.md"


def test_recursive_ingest_with_pages_and_tables(tmp_path: Path):
    from app.data.store import DataStore

    chunks = ingest.load_chunks(str(SAMPLE))
    files = {c.file for c in chunks}
    assert "documents/Office_Lease_Summary.docx" in files
    assert any(c.page == 2 for c in chunks if c.file.endswith("Project_Status_Riverside_Renovation.pdf"))
    store = DataStore(str(tmp_path / "t.duckdb"))
    try:
        loaded = ingest.load_tables(store, str(SAMPLE))
        assert {"bank_statement_2026_q2", "project_budget_riverside"} <= set(loaded)
        assert store.file_tables == set(loaded)
    finally:
        store.close()


def test_reserved_table_names_are_prefixed():
    assert ingest._safe_table_name("invoices") == "file_invoices"
    assert ingest._safe_table_name("qbo_bills") == "file_qbo_bills"
    assert ingest._safe_table_name("2026 budget") == "t_2026_budget"
