"""Invoice registry: extraction results + human review state, exposed as the `invoices` SQL table.

- Extractions are cached by file hash, so reindexing never re-runs OCR/AI on unchanged invoices.
- Review decisions (approve / reject / field corrections) are stored locally by file hash, so they
  survive restarts and renames. Accounting outputs are drafts until a named person approves them.
- Duplicates (same supplier + invoice number, or byte-identical files) are flagged across the batch.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.documents.parsers import ParsedDocument
from app.invoices.extract import (
    AMOUNT_FIELDS,
    DATE_FIELDS,
    FIELDS,
    InvoiceExtraction,
    extract_invoice,
    looks_like_invoice,
    match_known_supplier,
    normalize_number,
    normalize_supplier,
    parse_date,
)

STATUSES = {"needs_review", "approved", "rejected"}


@dataclass
class InvoiceRecord:
    id: str
    file: str
    sha256: str
    values: dict[str, Any]
    field_confidence: dict[str, float]
    field_notes: dict[str, str]
    field_evidence: dict[str, str]
    confidence: float
    issues: list[str]
    method: str
    ocr: bool
    status: str = "needs_review"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    corrected_fields: list[str] = field(default_factory=list)
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            **self.values,
            "confidence": self.confidence,
            "field_confidence": self.field_confidence,
            "field_notes": self.field_notes,
            "field_evidence": self.field_evidence,
            "issues": self.issues,
            "method": self.method,
            "ocr": self.ocr,
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "review_note": self.review_note,
            "corrected_fields": self.corrected_fields,
            "duplicate_of": self.duplicate_of,
        }


def is_invoice_document(doc: ParsedDocument) -> bool:
    """Files under an `invoices/` folder are invoices; anything else is classified by its content."""
    parts = [p.lower() for p in doc.file.split("/")[:-1]]
    if any("invoice" in p for p in parts):
        return True
    if any(p in ("documents", "docs", "contracts") for p in parts):
        return False
    return looks_like_invoice(doc.text)


class InvoiceRegistry:
    def __init__(self, reviews_path: str | Path | None, date_order: str = "MDY"):
        self.reviews_path = Path(reviews_path) if reviews_path else None
        self.date_order = date_order
        self._reviews: dict[str, dict[str, Any]] = self._load_reviews()
        self._cache: dict[tuple[str, bool], InvoiceExtraction] = {}
        self._lock = threading.RLock()
        self.records: list[InvoiceRecord] = []

    # ---- persistence ---------------------------------------------------
    def _load_reviews(self) -> dict[str, dict[str, Any]]:
        if self.reviews_path and self.reviews_path.exists():
            try:
                return json.loads(self.reviews_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_reviews(self) -> None:
        if not self.reviews_path:
            return
        self.reviews_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.reviews_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._reviews, indent=2), encoding="utf-8")
        tmp.replace(self.reviews_path)

    # ---- build ---------------------------------------------------------
    def build(
        self, docs: list[ParsedDocument], llm: Any = None, known_suppliers: list[str] | None = None
    ) -> list[InvoiceRecord]:
        records: list[InvoiceRecord] = []
        use_llm = llm is not None
        for doc in docs:
            if not is_invoice_document(doc):
                continue
            key = (doc.sha256, use_llm)
            ex = self._cache.get(key)
            if ex is None:
                ex = extract_invoice(
                    doc.text, ocr=doc.used_ocr, warnings=doc.warnings, llm=llm, date_order=self.date_order
                )
                self._cache[key] = ex
            rec = self._record(doc, ex)
            if known_suppliers and rec.values["supplier"]:
                # Canonicalise to the accounting system's vendor name (cheap; not cached with the extraction).
                fv = match_known_supplier(replace(ex.fields["supplier"]), known_suppliers)
                rec.values["supplier"] = fv.value
                rec.field_confidence["supplier"] = round(min(fv.confidence, 0.85) if ex.ocr else fv.confidence, 2)
                if fv.note:
                    rec.field_notes["supplier"] = fv.note
                rec.confidence = round(min(rec.field_confidence[f] if rec.values[f] is not None else 0.0
                                           for f in ("supplier", "invoice_number", "invoice_date", "total")), 2)
            records.append(rec)
        self._flag_duplicates(records)
        with self._lock:
            for rec in records:
                self._apply_review(rec)
            self.records = records
        return records

    @staticmethod
    def _record(doc: ParsedDocument, ex: InvoiceExtraction) -> InvoiceRecord:
        return InvoiceRecord(
            id=doc.sha256[:12],
            file=doc.file,
            sha256=doc.sha256,
            values={f: ex.fields[f].value for f in FIELDS},
            field_confidence={f: round(ex.fields[f].confidence, 2) for f in FIELDS},
            field_notes={f: ex.fields[f].note for f in FIELDS if ex.fields[f].note},
            field_evidence={f: ex.fields[f].evidence for f in FIELDS if ex.fields[f].evidence},
            confidence=ex.confidence,
            issues=list(ex.issues),
            method=ex.method,
            ocr=ex.ocr,
        )

    @staticmethod
    def _flag_duplicates(records: list[InvoiceRecord]) -> None:
        seen_key: dict[tuple[str, str], InvoiceRecord] = {}
        seen_sha: dict[str, InvoiceRecord] = {}
        for rec in records:
            original = seen_sha.get(rec.sha256)
            if original is None and rec.values["supplier"] and rec.values["invoice_number"]:
                key = (normalize_supplier(rec.values["supplier"]), normalize_number(rec.values["invoice_number"]))
                original = seen_key.get(key)
                seen_key.setdefault(key, rec)
            seen_sha.setdefault(rec.sha256, rec)
            if original is not None:
                rec.duplicate_of = original.file
                rec.issues.append(
                    f"Possible duplicate of {original.file} (same supplier and invoice number). Do not pay twice."
                )

    def _apply_review(self, rec: InvoiceRecord) -> None:
        review = self._reviews.get(rec.sha256)
        if not review:
            return
        for name, value in (review.get("corrections") or {}).items():
            if name in rec.values:
                rec.values[name] = value
                rec.field_confidence[name] = 1.0
                rec.field_notes[name] = f"corrected by {review.get('reviewed_by') or 'reviewer'}"
        rec.corrected_fields = sorted((review.get("corrections") or {}).keys())
        rec.status = review.get("status", "needs_review")
        rec.reviewed_by = review.get("reviewed_by")
        rec.reviewed_at = review.get("reviewed_at")
        rec.review_note = review.get("note")
        if rec.corrected_fields:
            required_ok = all(rec.values.get(f) is not None for f in ("supplier", "invoice_number", "invoice_date", "total"))
            rec.confidence = 1.0 if required_ok else rec.confidence

    # ---- review --------------------------------------------------------
    def get(self, record_id: str) -> InvoiceRecord | None:
        with self._lock:
            return next((r for r in self.records if r.id == record_id), None)

    def review(
        self, record_id: str, status: str, reviewer: str, corrections: dict[str, Any] | None = None, note: str = ""
    ) -> InvoiceRecord:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}")
        if not reviewer.strip():
            raise ValueError("a reviewer name is required")
        rec = self.get(record_id)
        if rec is None:
            raise KeyError(record_id)
        clean = self._clean_corrections(corrections or {})
        with self._lock:
            previous = self._reviews.get(rec.sha256, {})
            merged = {**(previous.get("corrections") or {}), **clean}
            self._reviews[rec.sha256] = {
                "file": rec.file,
                "status": status,
                "reviewed_by": reviewer.strip()[:80],
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": (note or "").strip()[:500],
                "corrections": merged,
            }
            self._save_reviews()
            self._apply_review(rec)
        return rec

    def _clean_corrections(self, corrections: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for name, value in corrections.items():
            if name not in FIELDS:
                raise ValueError(f"unknown field: {name}")
            if value in ("", None):
                continue
            if name in AMOUNT_FIELDS:
                try:
                    value = round(float(str(value).replace(",", "").replace("$", "")), 2)
                except ValueError as exc:
                    raise ValueError(f"{name} must be a number") from exc
            elif name in DATE_FIELDS:
                parsed = parse_date(str(value), self.date_order)
                if not parsed:
                    raise ValueError(f"{name} must be a date (YYYY-MM-DD)")
                value = parsed.iso
            else:
                value = str(value).strip()[:120]
            clean[name] = value
        return clean

    # ---- export --------------------------------------------------------
    def dataframe(self) -> pd.DataFrame:
        cols = ["id", "file", *FIELDS, "confidence", "status", "issue_count", "issues", "extraction_method",
                "ocr_used", "duplicate_of", "reviewed_by"]
        rows = []
        with self._lock:
            for r in self.records:
                rows.append(
                    {
                        "id": r.id,
                        "file": r.file,
                        **r.values,
                        "confidence": r.confidence,
                        "status": r.status,
                        "issue_count": len(r.issues),
                        "issues": " | ".join(r.issues),
                        "extraction_method": r.method,
                        "ocr_used": r.ocr,
                        "duplicate_of": r.duplicate_of,
                        "reviewed_by": r.reviewed_by,
                    }
                )
        df = pd.DataFrame(rows, columns=cols)
        for name in DATE_FIELDS:
            df[name] = pd.to_datetime(df[name], errors="coerce").dt.date
        for name in AMOUNT_FIELDS:
            df[name] = pd.to_numeric(df[name], errors="coerce")
        return df
