"""Bank statement ↔ QuickBooks matching: is every payment accounted for, and is every "paid" item real?

Bank lines come from any spreadsheet table classified as bank data (e.g. `bank_statement_2026_q2`),
with flexible column names (date / description / reference / debit / credit / amount).

    money OUT  → matched to a QuickBooks bill marked paid (amount equal, vendor or bill number on the line,
                 paid on/after the bill date)                                   → `bill_payment`
    money IN   → matched to a customer invoice payment (amount = paid portion, customer or invoice number)
                                                                                 → `customer_receipt`
    unmatched OUT → `no_bill` ("check it is recorded as an expense in QuickBooks": payroll, utilities, fees)
    unmatched IN  → `unidentified_receipt`
    QuickBooks bills/invoices marked paid with no bank line → `paid_without_bank_evidence`

Results land in the `bank_reconciliation` table. Nothing is changed anywhere.
"""

from __future__ import annotations

import difflib
import re
from datetime import date
from typing import Any

import pandas as pd

from app.data.ingest import RESERVED_PREFIXES, RESERVED_TABLES
from app.data.store import DataStore
from app.invoices.extract import normalize_number, normalize_supplier

SEVERITY = {
    "bill_payment": "ok",
    "customer_receipt": "ok",
    "opening_balance": "ok",
    "no_bill": "warning",
    "unidentified_receipt": "warning",
    "paid_without_bank_evidence": "issue",
}

_COLS = {
    "date": ("date", "txn_date", "transaction_date", "posted", "posting_date", "value_date"),
    "description": ("description", "details", "memo", "narrative", "payee", "particulars"),
    "reference": ("reference", "ref", "cheque", "check_no"),
    "debit": ("debit", "withdrawal", "withdrawals", "money_out", "paid_out", "out"),
    "credit": ("credit", "deposit", "deposits", "money_in", "paid_in", "in"),
    "amount": ("amount", "value"),
    "balance": ("balance", "running_balance"),
}


def _pick(columns: list[str], role: str) -> str | None:
    low = {c.lower(): c for c in columns}
    return next((low[n] for n in _COLS[role] if n in low), None)


def bank_tables(store: DataStore) -> list[str]:
    """Tables that look like bank statements. Tables the app writes itself (reconciliation output,
    extracted invoices, QuickBooks copies) are never statements, even if their names say "bank"."""
    out = []
    for t in store.tables():
        if t in RESERVED_TABLES or t.startswith(RESERVED_PREFIXES):
            continue
        if not any(w in t.lower() for w in ("bank", "statement", "transaction")):
            continue
        cols = [c for c, _ in store.schema().get(t, [])]
        if _pick(cols, "date") and (_pick(cols, "amount") or _pick(cols, "debit") or _pick(cols, "credit")):
            out.append(t)
    return out


def _num(v: Any) -> float:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def _date(v: Any) -> date | None:
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def load_bank_lines(store: DataStore) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for table in bank_tables(store):
        cols, rows = store.run_select(f'SELECT * FROM "{table}"', max_rows=100000)
        c = {role: _pick(cols, role) for role in _COLS}
        for i, r in enumerate(rows):
            row = dict(zip(cols, r, strict=False))
            if c["amount"]:
                amount = _num(row.get(c["amount"]))
            else:
                amount = _num(row.get(c["credit"])) - _num(row.get(c["debit"]))
            desc = str(row.get(c["description"]) or "") if c["description"] else ""
            lines.append({
                "table": table, "line": i + 1, "date": _date(row.get(c["date"])),
                "description": desc, "reference": str(row.get(c["reference"]) or "") if c["reference"] else "",
                "amount": round(amount, 2),
                "balance": _num(row.get(c["balance"])) if c["balance"] else None,
            })
    return lines


def _mentions(text: str, name: str | None) -> bool:
    if not name:
        return False
    t = normalize_supplier(text)
    n = normalize_supplier(name)
    if not n:
        return False
    if n in t:
        return True
    words = [w for w in n.split() if len(w) > 3]
    return bool(words) and sum(w in t for w in words) >= max(1, round(0.6 * len(words))) or \
        difflib.SequenceMatcher(None, n, t[: len(n) + 10]).ratio() > 0.8


def _mentions_number(text: str, number: str | None) -> bool:
    return bool(number) and normalize_number(number) in normalize_number(text) and len(normalize_number(number)) >= 3


def reconcile_bank(store: DataStore) -> list[dict[str, Any]]:
    lines = load_bank_lines(store)
    tables = set(store.tables())
    bills: list[dict[str, Any]] = []
    invoices: list[dict[str, Any]] = []
    if "qbo_bills" in tables:
        cols, rows = store.run_select("SELECT * FROM qbo_bills", max_rows=100000)
        bills = [dict(zip(cols, r, strict=False)) for r in rows]
    if "qbo_invoices" in tables:
        cols, rows = store.run_select("SELECT * FROM qbo_invoices", max_rows=100000)
        invoices = [dict(zip(cols, r, strict=False)) for r in rows]

    paid_bills = [b for b in bills if _num(b.get("total")) - _num(b.get("balance")) > 0.009]
    receipts = [i for i in invoices if _num(i.get("total")) - _num(i.get("balance")) > 0.009]
    used_bills: set[str] = set()
    used_inv: set[str] = set()
    out: list[dict[str, Any]] = []

    def row(line: dict | None, status: str, detail: str, qbo_type: str | None = None, qbo: dict | None = None) -> dict:
        return {
            "bank_table": line["table"] if line else None,
            "bank_line": line["line"] if line else None,
            "date": line["date"] if line else _date((qbo or {}).get("txn_date")),
            "description": line["description"] if line else None,
            "reference": line["reference"] if line else None,
            "amount": line["amount"] if line else None,
            "qbo_type": qbo_type,
            "qbo_id": (qbo or {}).get("id"),
            "qbo_doc_number": (qbo or {}).get("doc_number"),
            "counterparty": (qbo or {}).get("vendor_name") or (qbo or {}).get("customer_name"),
            "status": status,
            "severity": SEVERITY[status],
            "detail": detail,
        }

    for ln in lines:
        text = f"{ln['description']} {ln['reference']}"
        if abs(ln["amount"]) < 0.005:
            if re.search(r"opening|brought forward|b/f", text, re.I):
                out.append(row(ln, "opening_balance", "Opening balance."))
            continue
        if ln["amount"] < 0:
            want = -ln["amount"]
            cands = [b for b in paid_bills if b["id"] not in used_bills
                     and abs(_num(b["total"]) - _num(b.get("balance")) - want) <= 0.01
                     and (_mentions(text, b.get("vendor_name")) or _mentions_number(text, b.get("doc_number")))
                     and (not ln["date"] or not _date(b.get("txn_date")) or ln["date"] >= _date(b.get("txn_date")))]
            if cands:
                b = cands[0]
                used_bills.add(b["id"])
                out.append(row(ln, "bill_payment", f"Pays QuickBooks bill {b['id']} ({b.get('vendor_name')} "
                                                   f"{b.get('doc_number') or ''}).", "bill", b))
            else:
                out.append(row(ln, "no_bill", "Money out with no matching paid bill in QuickBooks. Check it is "
                                              "recorded (e.g. as an expense or payroll entry)."))
        else:
            cands = [i for i in receipts if i["id"] not in used_inv
                     and abs(_num(i["total"]) - _num(i.get("balance")) - ln["amount"]) <= 0.01
                     and (_mentions(text, i.get("customer_name")) or _mentions_number(text, i.get("doc_number")))]
            if cands:
                inv = cands[0]
                used_inv.add(inv["id"])
                out.append(row(ln, "customer_receipt", f"Payment for customer invoice {inv.get('doc_number')} "
                                                       f"({inv.get('customer_name')}).", "invoice", inv))
            else:
                out.append(row(ln, "unidentified_receipt", "Money in that doesn't match a customer invoice payment."))

    for b in paid_bills:
        if b["id"] not in used_bills:
            out.append(row(None, "paid_without_bank_evidence",
                           f"QuickBooks shows bill {b['id']} as paid ({_num(b['total']) - _num(b.get('balance')):,.2f}) "
                           "but no matching payment is on the bank statement.", "bill", b))
    for i in receipts:
        if i["id"] not in used_inv:
            out.append(row(None, "paid_without_bank_evidence",
                           f"QuickBooks shows invoice {i.get('doc_number')} as (part-)paid but no matching receipt is "
                           "on the bank statement.", "invoice", i))
    return out


def load_bank_reconciliation(store: DataStore, rows: list[dict[str, Any]]) -> int:
    cols = ["bank_table", "bank_line", "date", "description", "reference", "amount", "qbo_type", "qbo_id",
            "qbo_doc_number", "counterparty", "status", "severity", "detail"]
    return store.load_dataframe("bank_reconciliation", pd.DataFrame(rows, columns=cols))
