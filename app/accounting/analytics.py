"""Deterministic business analytics for the dashboard and reports. Every figure comes from SQL over
local tables (QuickBooks copy, extracted invoices, bank statement, spreadsheets), never from a model.

All date-relative figures (aging, overdue) use an as-of date: REPORT_AS_OF if set, otherwise today.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from typing import Any

import pandas as pd

from app.data.store import DataStore

AGING_BUCKETS = ["Not yet due", "1–30 days", "31–60 days", "61–90 days", "90+ days"]


def as_of_date(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date.today()


# Failed queries are never shown as "nothing found" or zero: they are logged, and collected here so the
# dashboard and reports can say which figures could not be computed.
QUERY_ERRORS: ContextVar[list[str] | None] = ContextVar("query_errors", default=None)


@contextmanager
def collect_query_errors() -> Iterator[list[str]]:
    errors: list[str] = []
    token = QUERY_ERRORS.set(errors)
    try:
        yield errors
    finally:
        QUERY_ERRORS.reset(token)


def _report(message: str) -> None:
    errors = QUERY_ERRORS.get()
    if errors is not None:
        errors.append(message)


def query(store: DataStore, sql: str, max_rows: int = 100_000) -> list[dict[str, Any]]:
    """Run app-written SQL. Errors and truncated results are reported, never passed off as complete figures."""
    try:
        cols, rows = store.run_select(sql, max_rows=max_rows, internal=True)
    except Exception as exc:
        from app.observability import event

        message = f"{type(exc).__name__}: {str(exc)[:200]}"
        event("query_failed", logging.WARNING, error=message)
        _report(message)
        return []
    if len(rows) >= max_rows:
        _report(f"a result was cut off at {max_rows:,} rows, so totals from it may be incomplete")
    return [dict(zip(cols, r, strict=False)) for r in rows]


def _q(store: DataStore, sql: str) -> list[dict[str, Any]]:
    return query(store, sql)


def _bucket(days: int) -> str:
    if days <= 0:
        return AGING_BUCKETS[0]
    if days <= 30:
        return AGING_BUCKETS[1]
    if days <= 60:
        return AGING_BUCKETS[2]
    if days <= 90:
        return AGING_BUCKETS[3]
    return AGING_BUCKETS[4]


def aging(store: DataStore, table: str, as_of: date) -> list[dict[str, Any]]:
    """Open balances by days past due (AP for qbo_bills, AR for qbo_invoices)."""
    out = {b: {"bucket": b, "amount": 0.0, "count": 0} for b in AGING_BUCKETS}
    if table not in store.tables():
        return list(out.values())
    for r in _q(store, f"SELECT due_date, balance FROM {table} WHERE balance > 0"):
        due = pd.to_datetime(r["due_date"], errors="coerce")
        days = (as_of - due.date()).days if not pd.isna(due) else 0
        b = out[_bucket(days)]
        b["amount"] = round(b["amount"] + float(r["balance"]), 2)
        b["count"] += 1
    return list(out.values())


def overdue_items(store: DataStore, table: str, as_of: date, name_col: str) -> list[dict[str, Any]]:
    if table not in store.tables():
        return []
    has_currency = any(c.lower() == "currency" for c, _ in store.schema().get(table, []))
    cur = "currency" if has_currency else "NULL AS currency"  # a bill in GBP must not be shown or totalled as USD
    rows = _q(store, f"SELECT {name_col} AS name, doc_number, due_date, balance, {cur} FROM {table} WHERE balance > 0")
    out = []
    for r in rows:
        due = pd.to_datetime(r["due_date"], errors="coerce")
        days = (as_of - due.date()).days if not pd.isna(due) else 0
        if days > 0:
            out.append({**r, "due_date": str(r["due_date"]), "days_overdue": days})
    return sorted(out, key=lambda r: -r["days_overdue"])


def spend_by_supplier(store: DataStore, top: int = 8) -> list[dict[str, Any]]:
    if "qbo_bills" not in store.tables():
        return []
    rows = _q(store, "SELECT vendor_name AS supplier, round(sum(total), 2) AS amount, count(*) AS bills "
                     "FROM qbo_bills GROUP BY vendor_name ORDER BY amount DESC")
    if len(rows) > top:
        rest = rows[top - 1:]
        rows = rows[: top - 1] + [{"supplier": f"Other ({len(rest)})", "amount": round(sum(r["amount"] for r in rest), 2),
                                   "bills": sum(r["bills"] for r in rest)}]
    return rows


def cash_flow(store: DataStore) -> dict[str, Any]:
    """Monthly money in/out and the running balance from the bank statement(s)."""
    from app.accounting.bank import load_bank_lines

    lines = [ln for ln in load_bank_lines(store) if ln["date"]]
    if not lines:
        return {"months": [], "balance": []}
    months: dict[str, dict[str, float]] = {}
    for ln in lines:
        m = ln["date"].strftime("%Y-%m")
        slot = months.setdefault(m, {"month": m, "money_in": 0.0, "money_out": 0.0})
        if ln["amount"] >= 0:
            slot["money_in"] = round(slot["money_in"] + ln["amount"], 2)
        else:
            slot["money_out"] = round(slot["money_out"] - ln["amount"], 2)
    balance = [{"date": ln["date"].isoformat(), "balance": ln["balance"]} for ln in lines if ln["balance"] is not None]
    return {"months": [months[k] for k in sorted(months)], "balance": balance}


def status_counts(store: DataStore, table: str) -> list[dict[str, Any]]:
    if table not in store.tables():
        return []
    return _q(store, f"SELECT status, severity, count(*) AS items FROM {table} GROUP BY status, severity "
                     "ORDER BY CASE severity WHEN 'issue' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, items DESC")


def budget_tables(store: DataStore) -> list[str]:
    out = []
    for t, cols in store.schema().items():
        names = {c.lower() for c, _ in cols}
        if "budget" in names and ({"spent", "actual"} & names):
            out.append(t)
    return out


def budget_vs_actual(store: DataStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in budget_tables(store):
        cols = {c.lower(): c for c, _ in store.schema()[t]}
        label = next((cols[c] for c in ("line_item", "item", "category", "description", "name") if c in cols), None)
        spent = cols.get("spent") or cols.get("actual")
        committed = cols.get("committed")
        if not label:
            continue
        sel = f'"{label}" AS item, "{cols["budget"]}" AS budget, "{spent}" AS spent' + (
            f', "{committed}" AS committed' if committed else ", NULL AS committed")
        for r in _q(store, f'SELECT {sel} FROM "{t}"'):
            rows.append({"project": re.sub(r"^project_budget_?", "", t).replace("_", " ").title() or t, **r})
    return rows


def invoice_stats(store: DataStore) -> dict[str, Any]:
    if "invoices" not in store.tables():
        return {}
    r = _q(store, "SELECT count(*) AS documents, count(*) FILTER (WHERE status = 'needs_review') AS needs_review, "
                  "count(*) FILTER (WHERE status = 'approved') AS approved, count(*) FILTER (WHERE issue_count > 0) AS flagged, "
                  "round(avg(confidence), 2) AS avg_confidence FROM invoices")
    return r[0] if r else {}


def kpis(store: DataStore, as_of: date) -> list[dict[str, Any]]:
    """Headline tiles. `tone` hints: good / warn / bad / neutral."""
    tiles: list[dict[str, Any]] = []
    inv = invoice_stats(store)
    if inv:
        tiles.append({"id": "review", "label": "Invoices awaiting review", "value": inv.get("needs_review", 0),
                      "unit": "count", "tone": "warn" if inv.get("needs_review") else "good",
                      "detail": f"{inv.get('flagged', 0)} flagged of {inv.get('documents', 0)}"})
    ap = aging(store, "qbo_bills", as_of)
    if "qbo_bills" in store.tables():
        open_ap = round(sum(b["amount"] for b in ap), 2)
        overdue_ap = round(sum(b["amount"] for b in ap[1:]), 2)
        tiles.append({"id": "ap", "label": "Open payables", "value": open_ap, "unit": "money", "tone": "neutral",
                      "detail": f"{sum(b['count'] for b in ap)} bills"})
        tiles.append({"id": "ap_overdue", "label": "Payables overdue", "value": overdue_ap, "unit": "money",
                      "tone": "bad" if overdue_ap else "good", "detail": f"as of {as_of.isoformat()}"})
    ar = aging(store, "qbo_invoices", as_of)
    if "qbo_invoices" in store.tables():
        overdue_ar = round(sum(b["amount"] for b in ar[1:]), 2)
        tiles.append({"id": "ar_overdue", "label": "Receivables overdue", "value": overdue_ar, "unit": "money",
                      "tone": "bad" if overdue_ar else "good",
                      "detail": f"of {round(sum(b['amount'] for b in ar), 2):,.2f} open"})
    cf = cash_flow(store)
    if cf["balance"]:
        tiles.append({"id": "cash", "label": "Bank balance", "value": cf["balance"][-1]["balance"], "unit": "money",
                      "tone": "neutral", "detail": f"at {cf['balance'][-1]['date']}"})
    issues = 0
    for t in ("invoice_reconciliation", "bank_reconciliation"):
        issues += sum(r["items"] for r in status_counts(store, t) if r["severity"] != "ok")
    if "invoice_reconciliation" in store.tables() or "bank_reconciliation" in store.tables():
        tiles.append({"id": "discrepancies", "label": "Items to check", "value": issues, "unit": "count",
                      "tone": "warn" if issues else "good", "detail": "invoice + bank reconciliation"})
    return tiles
