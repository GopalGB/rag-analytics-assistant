"""Reconcile extracted supplier invoices against QuickBooks bills to surface discrepancies.

For each invoice document: matched / amount_mismatch / possible_match / not_in_quickbooks / duplicate.
For each QuickBooks bill with no supporting document: no_document.
Results land in the `invoice_reconciliation` table and are shown for human review — nothing is
changed in QuickBooks.
"""

from __future__ import annotations

import difflib
from datetime import date
from typing import Any

import pandas as pd

from app.data.store import DataStore
from app.invoices.extract import normalize_number, normalize_supplier
from app.invoices.registry import InvoiceRecord

SEVERITY = {
    "matched": "ok",
    "amount_mismatch": "issue",
    "currency_mismatch": "issue",
    "possible_match": "warning",
    "not_in_quickbooks": "warning",
    "duplicate": "issue",
    "no_document": "warning",
    "rejected": "ok",
}


def _same_vendor(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na, nb = normalize_supplier(a), normalize_supplier(b)
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def _currency_conflict(doc: Any, bill: dict[str, Any]) -> tuple[str, str] | None:
    """(invoice currency, bill currency) when both are known and differ; equal numbers in two currencies are not
    the same amount."""
    a, b = str(doc or "").strip().upper(), str(bill.get("currency") or "").strip().upper()
    return (a, b) if a and b and a != b else None


def _days_apart(a: Any, b: Any) -> int:
    try:
        return abs((date.fromisoformat(str(a)) - date.fromisoformat(str(b))).days)
    except ValueError:
        return 9999


def _bills(store: DataStore) -> list[dict[str, Any]]:
    if "qbo_bills" not in store.tables():
        return []
    cols, rows = store.read_all("SELECT * FROM qbo_bills")
    return [dict(zip(cols, r, strict=False)) for r in rows]


def reconcile(records: list[InvoiceRecord], store: DataStore) -> list[dict[str, Any]]:
    bills = _bills(store)
    used: set[str] = set()
    out: list[dict[str, Any]] = []

    def row(rec: InvoiceRecord | None, bill: dict | None, status: str, detail: str) -> dict[str, Any]:
        doc_total = rec.values.get("total") if rec else None
        qbo_total = float(bill["total"]) if bill and bill.get("total") is not None else None
        # amounts in two different currencies are never subtracted (no conversion is done here)
        comparable = status != "currency_mismatch"
        diff = (round(doc_total - qbo_total, 2)
                if comparable and doc_total is not None and qbo_total is not None else None)
        return {
            "source": "document" if rec else "quickbooks",
            "file": rec.file if rec else None,
            "invoice_id": rec.id if rec else None,
            "supplier": (rec.values.get("supplier") if rec else None) or (bill or {}).get("vendor_name"),
            "invoice_number": (rec.values.get("invoice_number") if rec else None) or (bill or {}).get("doc_number"),
            "invoice_date": (rec.values.get("invoice_date") if rec else None) or (bill or {}).get("txn_date"),
            "document_total": doc_total,
            # the invoice's own currency, else the QuickBooks bill's (a bill with no document still has one)
            "currency": ((rec.values.get("currency") if rec else None) or (bill or {}).get("currency") or None),
            "qbo_bill_id": (bill or {}).get("id"),
            "qbo_total": qbo_total,
            "qbo_balance": float(bill["balance"]) if bill and bill.get("balance") is not None else None,
            "difference": diff,
            "status": status,
            "severity": SEVERITY[status],
            "detail": detail,
        }

    for rec in records:
        v = rec.values
        if rec.status == "rejected":
            out.append(row(rec, None, "rejected", "Rejected by a reviewer; not reconciled."))
            continue
        if rec.duplicate_of:
            out.append(row(rec, None, "duplicate", f"Duplicate of {rec.duplicate_of}. Make sure it is not paid twice."))
            continue
        number = normalize_number(v["invoice_number"]) if v.get("invoice_number") else None
        candidates = [b for b in bills if b["id"] not in used]
        exact = [
            b for b in candidates
            if number and b.get("doc_number") and normalize_number(b["doc_number"]) == number
            and _same_vendor(v.get("supplier"), b.get("vendor_name"))
        ]
        if exact:
            bill = exact[0]
            used.add(bill["id"])
            total = v.get("total")
            conflict = _currency_conflict(v.get("currency"), bill)
            if conflict:
                out.append(row(rec, bill, "currency_mismatch",
                               f"QuickBooks bill {bill['id']} is in {conflict[1]} but the invoice is in {conflict[0]}; "
                               "check which is right before comparing amounts."))
            elif total is not None and abs(float(bill["total"]) - total) <= 0.01:
                paid = "paid" if not bill.get("balance") else f"open balance {float(bill['balance']):,.2f}"
                out.append(row(rec, bill, "matched", f"Recorded in QuickBooks as bill {bill['id']}; amounts agree ({paid})."))
            else:
                shown = f"{total:,.2f}" if total is not None else "an unreadable amount"
                out.append(
                    row(
                        rec, bill, "amount_mismatch",
                        f"QuickBooks bill {bill['id']} is {float(bill['total']):,.2f} but the invoice says {shown}.",
                    )
                )
            continue
        # Fuzzy: same vendor + same amount within 7 days (e.g. invoice number missing or mistyped).
        fuzzy = [
            b for b in candidates
            if _same_vendor(v.get("supplier"), b.get("vendor_name"))
            and v.get("total") is not None and abs(float(b["total"]) - v["total"]) <= 0.01
            and _days_apart(v.get("invoice_date"), b.get("txn_date")) <= 7
            and not _currency_conflict(v.get("currency"), b)  # same number in another currency is no match
        ]
        if fuzzy:
            bill = fuzzy[0]
            used.add(bill["id"])
            why = "the invoice number is missing on the document" if not number else "the invoice numbers differ"
            out.append(
                row(rec, bill, "possible_match", f"Probably QuickBooks bill {bill['id']} (same supplier, amount and date), but {why}.")
            )
            continue
        out.append(row(rec, None, "not_in_quickbooks", "No matching bill found in QuickBooks. It may need to be recorded."))

    for bill in bills:
        if bill["id"] not in used:
            out.append(row(None, bill, "no_document", "Bill exists in QuickBooks but no supporting invoice document is on file."))
    return out


def load_reconciliation(store: DataStore, rows: list[dict[str, Any]]) -> int:
    cols = [
        "source", "file", "invoice_id", "supplier", "invoice_number", "invoice_date", "document_total", "currency",
        "qbo_bill_id", "qbo_total", "qbo_balance", "difference", "status", "severity", "detail",
    ]
    df = pd.DataFrame(rows, columns=cols)
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce").dt.date
    return store.load_dataframe("invoice_reconciliation", df)
