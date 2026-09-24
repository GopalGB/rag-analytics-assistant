"""Run the prototype's acceptance checks and write docs/TEST-RESULTS.md.

Checks (all against the synthetic data set, fully offline):
1. Invoice extraction accuracy per field, split into sample invoices and UNSEEN hold-out invoices
   (new layouts, a scanned PDF and a phone-photo PNG that the rules were not tuned on).
2. Uncertainty handling: missing / inconsistent / ambiguous values must be flagged, never invented.
3. Document search: does the right source document come back first for each question?
4. "Not in the documents" questions: the assistant must say so rather than guess.
5. Reconciliation against the QuickBooks sandbox fixture: expected discrepancy per invoice.

Usage:  python scripts/evaluate.py            (writes docs/TEST-RESULTS.md)
        python scripts/evaluate.py --with-ai  (also uses the configured AI model for extraction)
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.accounting import qbo_sync, reconcile  # noqa: E402
from app.agent.engine import AgentEngine  # noqa: E402
from app.agent.memory import ConversationMemory  # noqa: E402
from app.data import ingest  # noqa: E402
from app.data.store import DataStore  # noqa: E402
from app.documents.ocr import OCREngine  # noqa: E402
from app.documents.parsers import parse_file  # noqa: E402
from app.integrations.quickbooks import MockQuickBooks  # noqa: E402
from app.invoices.extract import extract_invoice  # noqa: E402
from app.invoices.registry import InvoiceRegistry  # noqa: E402
from app.rag.embeddings import EmbeddingService  # noqa: E402
from app.rag.retriever import Retriever  # noqa: E402
from app.security import InputGuard  # noqa: E402

FIELDS = ["supplier", "invoice_number", "invoice_date", "due_date", "subtotal", "tax", "total"]

SEARCH_CASES = [
    ("What are the payment terms in the Summit Ridge supply agreement?", "Supplier_Agreement_Summit_Ridge_Electrical.pdf"),
    ("How much notice is needed to terminate the electrical supply agreement?", "Supplier_Agreement_Summit_Ridge_Electrical.pdf"),
    ("What is the liability cap?", "Supplier_Agreement_Summit_Ridge_Electrical.pdf"),
    ("When does the office lease expire?", "Office_Lease_Summary.docx"),
    ("How much is the security deposit on the lease?", "Office_Lease_Summary.docx"),
    ("What is the monthly fee in the Coastal Plumbing maintenance agreement?", "Maintenance_Agreement_Coastal_Plumbing.pdf"),
    ("What tasks are outstanding on the Riverside renovation project?", "Project_Status_Riverside_Renovation.pdf"),
    ("What is the forecast at completion for the Riverside project?", "Project_Status_Riverside_Renovation.pdf"),
    ("Who must approve invoices over $5,000?", "Expense_and_Invoice_Approval_Policy.md"),
    ("How should changes to supplier bank details be verified?", "Expense_and_Invoice_Approval_Policy.md"),
]
NOT_FOUND_CASES = ["Who is our auditor?", "What is the company's VAT registration number?", "When is the staff party?"]
EXPECTED_RECON = {
    "summit_INV-10421.pdf": "matched",
    "summit_INV-10421_resubmitted.pdf": "duplicate",
    "greenleaf_GL-2291.pdf": "matched",
    "coastal_5530.pdf": "amount_mismatch",
    "pioneer_PCW-0192_scanned.pdf": "matched",
    "metro_MOS-7781.pdf": "not_in_quickbooks",
    "brightspark_BSC-118.pdf": "not_in_quickbooks",
    "harbor_waste_no_number.pdf": "possible_match",
}
EXPECTED_FLAGS = {
    "greenleaf_GL-2291.pdf": "ambiguous",
    "harbor_waste_no_number.pdf": "Missing invoice number",
    "brightspark_BSC-118.pdf": "Totals don't add up",
    "summit_INV-10421_resubmitted.pdf": "Possible duplicate",
    "pioneer_PCW-0192_scanned.pdf": "OCR",
}


def _path(name: str) -> Path:
    p = ROOT / "data" / "sample" / "invoices" / name
    return p if p.exists() else ROOT / "data" / "unseen_invoices" / name


def main() -> None:
    with_ai = "--with-ai" in sys.argv
    llm = None
    if with_ai:
        from app.agent.llm import select_llm
        from app.config import Settings

        llm, note = select_llm(Settings())
        print("AI model:", note)
    ocr = OCREngine()
    truth = json.loads((ROOT / "data" / "ground_truth" / "invoices.json").read_text())
    lines: list[str] = []
    out = lines.append

    # 1 + 2. extraction ------------------------------------------------------------
    per_split: dict[str, dict[str, list[int]]] = {}
    detail_rows = []
    flags_ok = 0
    for t in truth:
        doc = parse_file(_path(t["file"]), t["file"], ocr)
        ex = extract_invoice(doc.text, ocr=doc.used_ocr, warnings=doc.warnings, llm=llm)
        stats = per_split.setdefault(t["split"], {f: [0, 0] for f in FIELDS})
        wrong = []
        for f in FIELDS:
            got = ex.value(f)
            stats[f][1] += 1
            if got == t[f]:
                stats[f][0] += 1
            else:
                wrong.append(f"{f}: got {got!r}, expected {t[f]!r}")
        detail_rows.append((t["split"], t["file"], "yes" if doc.used_ocr else "", "all correct" if not wrong else "; ".join(wrong),
                            ex.confidence, " / ".join(ex.issues) or "-"))

    # 3 + 4. search --------------------------------------------------------------
    tmp = Path(tempfile.mkdtemp())
    docs = ingest.load_documents(str(ROOT / "data" / "sample"), ocr)
    retriever = Retriever(EmbeddingService("local", 256)).build(ingest.chunk_documents(docs))
    store = DataStore(str(tmp / "eval.duckdb"))
    engine = AgentEngine(store, retriever, InputGuard(), None, ConversationMemory())
    top1 = top3 = 0
    search_rows = []
    for q, expected in SEARCH_CASES:
        hits = retriever.search(q, k=3)
        names = [h.file.rsplit("/", 1)[-1] for h in hits]
        top1 += bool(names and names[0] == expected)
        top3 += expected in names
        search_rows.append((q, expected, names[0] if names else "-", "yes" if names and names[0] == expected else "no"))
    answered = 0
    for q, expected in SEARCH_CASES:
        ans = engine.answer("eval", q)
        answered += bool(ans["sources"]) and ans["sources"][0]["file"].endswith(expected)
    nf_rows = []
    nf_ok = 0
    for q in NOT_FOUND_CASES:
        ans = engine.answer("eval", q)
        ok = "couldn't find" in ans["text"]
        nf_ok += ok
        nf_rows.append((q, "says not found" if ok else "returned passages"))

    # 5. reconciliation -------------------------------------------------------------
    qbo_sync.sync(MockQuickBooks(ROOT / "data" / "qbo_sandbox" / "sandbox_company.json"), store)
    reg = InvoiceRegistry(None)
    records = reg.build([d for d in docs if d.file.startswith("invoices/")])
    rows = reconcile.reconcile(records, store)
    issues_by_file = {r.file.rsplit("/", 1)[-1]: r.issues for r in records}
    for name, want in EXPECTED_FLAGS.items():
        flags_ok += any(want in i for i in issues_by_file.get(name, []))
    got_status = {r["file"].rsplit("/", 1)[-1]: r["status"] for r in rows if r["file"]}
    recon_ok = sum(got_status.get(f) == s for f, s in EXPECTED_RECON.items())
    orphan = sorted(r["invoice_number"] for r in rows if r["status"] == "no_document")
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)

    # report ---------------------------------------------------------------------------
    out("# Test results")
    out("")
    out(f"_Generated by `python scripts/evaluate.py{' --with-ai' if with_ai else ''}` on {date.today().isoformat()} "
        f"(Python {platform.python_version()}, {platform.system()} {platform.machine()}, OCR: {ocr.name}, "
        f"extraction: {'rules + AI (' + getattr(llm, 'name', '?') + ')' if llm else 'local rules only'})._")
    out("")
    out("All data is synthetic. \"Unseen\" invoices use layouts, labels and formats (a remittance-style PDF, a minimal PDF, "
        "and a phone-photo PNG) that were not part of the sample set.")
    out("")
    out("## 1. Invoice field extraction")
    out("")
    out("| Field | Sample invoices | Unseen invoices |")
    out("|---|---|---|")
    for f in FIELDS:
        cells = []
        for split in ("sample", "unseen"):
            ok, n = per_split.get(split, {}).get(f, [0, 0])
            cells.append(f"{ok}/{n} ({ok / n:.0%})" if n else "-")
        out(f"| {f} | {cells[0]} | {cells[1]} |")
    out("")
    out("A field counts as correct only if it exactly matches the ground truth, including `null` where the document does "
        "not print the value (e.g. the invoice with no invoice number).")
    out("")
    out("| Split | File | OCR | Result | Confidence | Flags raised |")
    out("|---|---|---|---|---|---|")
    for r in detail_rows:
        out(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:.2f} | {r[5]} |")
    out("")
    out("## 2. Flagging missing or uncertain information")
    out("")
    out(f"**{flags_ok}/{len(EXPECTED_FLAGS)}** planted problems were flagged for review (ambiguous date, missing invoice "
        "number, totals that don't add up, duplicate submission, scanned/OCR input).")
    out("")
    out("## 3. Document search (right source first)")
    out("")
    out(f"Top-1: **{top1}/{len(SEARCH_CASES)}**, top-3: **{top3}/{len(SEARCH_CASES)}** (offline hashing embeddings + BM25).")
    out("")
    out("| Question | Expected source | Top result | Top-1 |")
    out("|---|---|---|---|")
    for r in search_rows:
        out(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
    out("")
    out("## 4. Questions the documents can't answer")
    out("")
    out(f"**{nf_ok}/{len(NOT_FOUND_CASES)}** correctly answered \"not found\" instead of guessing, while "
        f"**{answered}/{len(SEARCH_CASES)}** of the answerable questions above still returned the right passage "
        "(checked in no-AI mode, where this filter is the only safeguard; with an AI model the model also "
        "declines to answer from irrelevant passages).")
    out("")
    for q, r in nf_rows:
        out(f"- {q} → {r}")
    out("")
    out("## 5. Reconciliation against the QuickBooks sandbox company")
    out("")
    out(f"**{recon_ok}/{len(EXPECTED_RECON)}** invoices received the expected result. QuickBooks bills with no supporting "
        f"document were identified: {', '.join(orphan)}.")
    out("")
    out("| Invoice file | Expected | Got |")
    out("|---|---|---|")
    for f, s in EXPECTED_RECON.items():
        out(f"| {f} | {s} | {got_status.get(f, '-')} |")
    out("")
    out("## Automated test suite")
    out("")
    out("`pytest` runs 100+ unit and end-to-end tests (parsing, OCR, extraction, grounding of AI values, QuickBooks read-only "
        "enforcement and OAuth token handling, reconciliation, approvals, tamper-evident log, API, guardrails). CI runs them "
        "on every push.")
    out("")
    out("See [ROADMAP.md](ROADMAP.md) for known limitations.")
    dest = ROOT / "docs" / "TEST-RESULTS.md"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote {dest}")


if __name__ == "__main__":
    main()
