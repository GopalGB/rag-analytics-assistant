import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

from pypdf import PdfReader

from app.data import ingest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample"


def test_generated_corpus_is_reproducible_and_readable(tmp_path):
    output = tmp_path / "sample"
    command = [sys.executable, "scripts/generate_sample_data.py", "--output", str(output)]
    subprocess.run(command, cwd=ROOT, check=True)
    files = sorted(path for path in output.iterdir() if path.is_file())
    assert len(files) >= 12
    assert (output / "sales.csv").exists()
    assert len(list(output.glob("invoice_*.txt"))) >= 5
    assert len(list(output.glob("invoice_pdf_*.pdf"))) == 3
    assert len(list(output.glob("*.pdf"))) >= 5
    assert len(list(output.glob("*.docx"))) >= 1
    assert "Procurement" in "\n".join(page.extract_text() or "" for page in PdfReader(output / "procurement_policy.pdf").pages)
    with zipfile.ZipFile(output / "escalation_policy.docx") as archive:
        assert "Escalate" in archive.read("word/document.xml").decode()
    before = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in files}
    subprocess.run(command, cwd=ROOT, check=True)
    after = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in output.iterdir() if path.is_file()}
    assert before == after


def test_generated_complete_invoices_have_all_displayed_fields():
    for path in sorted(SAMPLE.glob("invoice_0*.txt")):
        invoice = ingest.extract_invoice(ingest.read_document(path), path.name)
        assert invoice["missing_fields"] == []
        assert invoice["review_fields"] == []
        assert {"supplier", "invoice_number", "date", "amount"} <= set(invoice["evidence"])


def test_generated_pdf_invoices_have_unique_complete_fields(tmp_path):
    output = tmp_path / "sample"
    subprocess.run([sys.executable, "scripts/generate_sample_data.py", "--output", str(output)], cwd=ROOT, check=True)
    invoices = [ingest.extract_invoice(ingest.read_document(path), path.name) for path in sorted(output.glob("invoice_pdf_*.pdf"))]
    assert [invoice["invoice_number"] for invoice in invoices] == ["INV-PDF001", "INV-PDF002", "INV-PDF003"]
    assert all(invoice["currency"] == "AED" and not invoice["missing_fields"] and not invoice["review_fields"] for invoice in invoices)
