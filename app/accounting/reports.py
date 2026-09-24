"""Deterministic accounts-payable / receivable summary. Every number comes from SQL, not a model.

The report is a DRAFT for a responsible person to review; it says so in its header.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.data.store import DataStore


def _q(store: DataStore, sql: str) -> list[dict[str, Any]]:
    try:
        cols, rows = store.run_select(sql, max_rows=1000)
    except Exception:
        return []
    return [dict(zip(cols, r, strict=False)) for r in rows]


def build_summary(store: DataStore) -> dict[str, Any]:
    tables = set(store.tables())
    out: dict[str, Any] = {"generated_at": datetime.now().isoformat(timespec="seconds"), "sections": {}}
    s = out["sections"]
    if "invoices" in tables:
        s["invoices"] = _q(
            store,
            "SELECT count(*) AS documents, "
            "count(*) FILTER (WHERE status = 'needs_review') AS needs_review, "
            "count(*) FILTER (WHERE status = 'approved') AS approved, "
            "count(*) FILTER (WHERE issue_count > 0) AS flagged, "
            "round(sum(total) FILTER (WHERE duplicate_of IS NULL AND status <> 'rejected'), 2) AS total_value "
            "FROM invoices",
        )[0]
        s["by_supplier"] = _q(
            store,
            "SELECT supplier, count(*) AS invoices, round(sum(total), 2) AS total FROM invoices "
            "WHERE duplicate_of IS NULL AND status <> 'rejected' GROUP BY supplier ORDER BY total DESC NULLS LAST",
        )
    if "invoice_reconciliation" in tables:
        s["reconciliation"] = _q(
            store,
            "SELECT status, severity, count(*) AS items FROM invoice_reconciliation GROUP BY status, severity "
            "ORDER BY CASE severity WHEN 'issue' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, status",
        )
        s["attention"] = _q(
            store,
            "SELECT status, supplier, invoice_number, document_total, qbo_total, detail FROM invoice_reconciliation "
            "WHERE severity <> 'ok' ORDER BY CASE severity WHEN 'issue' THEN 0 ELSE 1 END, supplier",
        )
    if "qbo_bills" in tables:
        s["payables"] = _q(
            store,
            "SELECT count(*) FILTER (WHERE balance > 0) AS open_bills, round(sum(balance), 2) AS open_balance, "
            "count(*) FILTER (WHERE days_overdue > 0) AS overdue_bills, "
            "round(sum(balance) FILTER (WHERE days_overdue > 0), 2) AS overdue_balance FROM qbo_bills",
        )[0]
    if "qbo_invoices" in tables:
        s["receivables"] = _q(
            store,
            "SELECT count(*) FILTER (WHERE balance > 0) AS open_invoices, round(sum(balance), 2) AS open_balance, "
            "round(sum(balance) FILTER (WHERE days_overdue > 0), 2) AS overdue_balance FROM qbo_invoices",
        )[0]
        s["overdue_receivables"] = _q(
            store,
            "SELECT customer_name, doc_number, due_date, balance, days_overdue FROM qbo_invoices "
            "WHERE days_overdue > 0 ORDER BY days_overdue DESC",
        )
    return out


def _money(v: Any) -> str:
    return "-" if v is None else f"${float(v):,.2f}"


def to_markdown(summary: dict[str, Any]) -> str:
    s = summary["sections"]
    lines = [
        "# Accounts summary (DRAFT)",
        "",
        f"_Generated {summary['generated_at']} from local data. For review by a responsible person before use._",
        "",
    ]
    if "invoices" in s:
        i = s["invoices"]
        lines += [
            "## Supplier invoices on file",
            f"- Documents: {i['documents']} ({i['needs_review']} awaiting review, {i['approved']} approved, "
            f"{i['flagged']} flagged)",
            f"- Total value (excluding duplicates/rejected): {_money(i['total_value'])}",
            "",
        ]
    if s.get("attention"):
        lines += ["## Needs attention", ""]
        for a in s["attention"]:
            lines.append(f"- **{a['status'].replace('_', ' ')}**: {a['supplier'] or '?'} {a['invoice_number'] or ''} - {a['detail']}")
        lines.append("")
    if "payables" in s:
        p = s["payables"]
        lines += [
            "## Payables (QuickBooks)",
            f"- Open bills: {p['open_bills']} totalling {_money(p['open_balance'])}",
            f"- Overdue: {p['overdue_bills']} totalling {_money(p['overdue_balance'])}",
            "",
        ]
    if "receivables" in s:
        r = s["receivables"]
        lines += [
            "## Receivables (QuickBooks)",
            f"- Open invoices: {r['open_invoices']} totalling {_money(r['open_balance'])}; overdue {_money(r['overdue_balance'])}",
        ]
        for o in s.get("overdue_receivables", []):
            lines.append(f"  - {o['customer_name']} #{o['doc_number']}: {_money(o['balance'])}, {o['days_overdue']} days overdue")
        lines.append("")
    return "\n".join(lines)
