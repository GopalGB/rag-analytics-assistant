"""Generate a small, fully synthetic demo dataset (no real-world data of any kind).

Writes a sales CSV and two short markdown docs into data/sample/ so the app and tests have
something to chew on out of the box. Deterministic via a fixed seed.
"""

from __future__ import annotations

import csv
import random
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "sample"

REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = {
    "Apparel": ["T-Shirt", "Jeans", "Jacket", "Socks"],
    "Home": ["Lamp", "Cushion", "Mug", "Towel"],
    "Electronics": ["Earbuds", "Charger", "Speaker", "Webcam"],
}


def write_sales(path: Path, rows: int = 4000, seed: int = 7) -> None:
    rng = random.Random(seed)
    products = [(p, cat) for cat, items in CATEGORIES.items() for p in items]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "region",
                "store_id",
                "category",
                "product",
                "week",
                "units_sold",
                "revenue",
                "revenue_last_year",
            ]
        )
        for _ in range(rows):
            product, category = rng.choice(products)
            region = rng.choice(REGIONS)
            store_id = rng.randint(100, 140)
            week = rng.randint(1, 52)
            units = rng.randint(0, 200)
            price = round(rng.uniform(5, 120), 2)
            revenue = round(units * price, 2)
            ly = round(revenue * rng.uniform(0.7, 1.4), 2)
            w.writerow([region, store_id, category, product, week, units, revenue, ly])


ONBOARDING = """# Onboarding Guide

Welcome to the analytics workspace. This tool answers questions about the sales dataset and
the reference documents loaded alongside it.

## How it works
Ask a natural-language question. The assistant decides whether to run a SQL query over the
tabular data, search the documents, or both, and then summarizes the result with sources.

## Tips
- Be specific about the metric and the grouping (for example: revenue by region for week 10).
- The assistant only runs read-only queries; it cannot modify data.
- If you need a definition, ask — definitions live in the reference documents.
"""

FORECASTING = """# Forecasting Overview

Forecasting estimates future demand from historical sales. This document defines the core terms.

## Key terms
- Baseline: the expected units sold under normal conditions.
- Lift: the incremental units attributable to a promotion or event.
- Seasonality: repeating weekly or yearly demand patterns.
- Year-over-year (YoY): this period compared with the same period last year.

## Method
A simple baseline-plus-lift model is sufficient for most categories. Review YoY revenue to
spot structural shifts before trusting a short-term forecast.
"""

PROCUREMENT = """# Synthetic Procurement Policy
This synthetic interview-demo document defines procurement approval. Purchases above $5,000 need finance approval; urgent exceptions must be documented and escalated to procurement. All vendors and examples in this file are fictional.
"""


def _pdf_bytes(text: str) -> bytes:
    lines = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").splitlines()
    stream = f"BT /F1 12 Tf 72 720 Td ({') Tj 0 -14 Td ('.join(lines)}) Tj ET".encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    start = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


def _docx_bytes(text: str) -> bytes:
    content = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{content}</w:t></w:r></w:p></w:body></w:document>'
    types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in (("[Content_Types].xml", types), ("_rels/.rels", rels), ("word/document.xml", document)):
            member = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, value)
    return buffer.getvalue()


def write_documents() -> None:
    for index in range(1, 6):
        quantity, unit_price = Decimal(index + 1), Decimal("125.50")
        total = quantity * unit_price
        (OUT / f"invoice_{index:02d}.txt").write_text(f"SYNTHETIC INTERVIEW-DEMO DATA\nInvoice Number: INV-{index:03d}\nVendor: Fictional Vendor {index}\nInvoice Date: 2026-10-{index:02d}\nCurrency: USD\nQuantity: {quantity}\nUnit price: USD {unit_price}\nTotal: USD {total}\nPayment due: 2026-10-{10 + index:02d}\n", encoding="utf-8")
    (OUT / "invoice_missing_total.txt").write_text("SYNTHETIC INTERVIEW-DEMO DATA\nInvoice INV-MISSING\nVendor: Fictional Vendor\nQuantity: 2\nUnit price: $10.00\nTotal: MISSING\n", encoding="utf-8")
    (OUT / "invoice_ambiguous.txt").write_text("SYNTHETIC INTERVIEW-DEMO DATA\nInvoice INV-AMBIGUOUS\nVendor: Fictional Vendor\nQuantity: two\nUnit price: negotiable\nTotal: unclear\n", encoding="utf-8")
    (OUT / "invoice_inconsistent.txt").write_text("SYNTHETIC INTERVIEW-DEMO DATA\nInvoice INV-INCONSISTENT\nVendor: Fictional Vendor\nQuantity: 3\nUnit price: $10.00\nTotal: $25.00\n", encoding="utf-8")
    (OUT / "procurement_policy.pdf").write_bytes(_pdf_bytes("SYNTHETIC INTERVIEW-DEMO DATA Procurement approvals above $5,000 require finance approval."))
    (OUT / "payment_policy.pdf").write_bytes(_pdf_bytes("SYNTHETIC INTERVIEW-DEMO DATA Payment terms are net 30; escalate overdue invoices to finance."))
    for index in range(1, 4):
        subtotal = Decimal(index * 100)
        tax = Decimal(index * 5)
        total = subtotal + tax
        (OUT / f"invoice_pdf_{index:03d}.pdf").write_bytes(
            _pdf_bytes(
                f"SYNTHETIC INTERVIEW-DEMO DATA\nSupplier: Fictional PDF Vendor {index}\n"
                f"Invoice Number: INV-PDF{index:03d}\nInvoice Date: 2026-09-{index:02d}\n"
                f"Currency: AED\nSubtotal: AED {subtotal:.2f}\nTax: AED {tax:.2f}\nGrand Total: AED {total:.2f}"
            )
        )
    (OUT / "escalation_policy.docx").write_bytes(_docx_bytes("SYNTHETIC INTERVIEW-DEMO DATA Escalate urgent procurement exceptions to procurement."))


def main(output: Path = OUT) -> None:
    global OUT
    OUT = output
    OUT.mkdir(parents=True, exist_ok=True)
    write_sales(OUT / "sales.csv")
    (OUT / "onboarding_guide.md").write_text(ONBOARDING, encoding="utf-8")
    (OUT / "forecasting_overview.md").write_text(FORECASTING, encoding="utf-8")
    (OUT / "procurement_policy.md").write_text(PROCUREMENT, encoding="utf-8")
    write_documents()
    print(f"Wrote sample data to {OUT}")


if __name__ == "__main__":
    output = Path(sys.argv[sys.argv.index("--output") + 1]) if "--output" in sys.argv else OUT
    main(output)
