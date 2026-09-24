"""What needs attention now: one ranked list across accounting, invoices, the bank and the documents.

Every item is computed from the local data (no AI model), carries its source (table or file + page), the
money at stake where there is one, and a suggested next step or question. Sources:

- invoice ↔ QuickBooks reconciliation problems (amount mismatches, duplicates, unrecorded invoices)
- invoices waiting for review that have flagged problems
- overdue customer invoices and supplier bills
- bank lines that don't match QuickBooks (e.g. a bill marked paid with no payment on the statement)
- budget lines committed beyond their budget
- deadlines read from the documents (overdue, due within 30 days)
- the company's own approval policy, read from its policy document (e.g. "Invoices over $5,000:
  approved by a director") and applied to invoices awaiting approval
- anomalies: an invoice far above the supplier's usual amount, or the same amount billed twice under
  different invoice numbers
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Any

from app.accounting.analytics import budget_vs_actual, overdue_items, query
from app.data.store import DataStore
from app.documents.deadlines import find_deadlines

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_LABELS = {
    "amount_mismatch": "amount differs from QuickBooks",
    "duplicate": "duplicate of an invoice already on file",
    "not_in_quickbooks": "not recorded in QuickBooks",
    "no_document": "QuickBooks bill with no invoice on file",
    "possible_match": "possible match, needs confirming",
    "paid_without_bank_evidence": "marked paid in QuickBooks, but no payment on the bank statement",
    "unidentified_receipt": "money received that matches no customer invoice",
    "no_bill": "payment with no bill in QuickBooks",
}


HOME_CURRENCY = "USD"  # amounts with no currency of their own (QuickBooks, bank, budget) are in this currency


def money(v: float | None, currency: str | None = None) -> str:
    """`$1,234.00` for US dollars, `EUR 1,234.00` for anything else. Amounts are never summed across currencies."""
    if v is None:
        return ""
    cur = (currency or HOME_CURRENCY).upper()
    return f"${v:,.2f}" if cur == "USD" else f"{cur} {v:,.2f}"


_money = money


def _item(severity: str, category: str, title: str, detail: str, source: dict[str, Any], amount: float | None = None,
          ask: str | None = None, link: str | None = None, currency: str | None = None) -> dict[str, Any]:
    key = f"{category}|{title}|{detail}|{source.get('name')}|{amount}"
    return {"id": hashlib.sha1(key.encode()).hexdigest()[:12], "severity": severity, "category": category,
            "title": title, "detail": detail, "amount": None if amount is None else round(float(amount), 2),
            "currency": (currency or HOME_CURRENCY).upper(), "source": source, "ask": ask, "link": link}


# --------------------------------------------------------------------------- policy read from documents
@dataclass
class ApprovalRule:
    low: float  # applies to amounts above this (exclusive) ...
    high: float  # ... up to this (inclusive)
    approver: str
    file: str


def approval_rules(documents: list) -> list[ApprovalRule]:
    """Approval thresholds written in the company's policy documents, e.g.
    "Invoices over $5,000: approved by a director, and two quotes must be on file"."""
    rules: list[ApprovalRule] = []
    num = r"\$?\s?([\d,]+(?:\.\d+)?)"
    for doc in documents:
        text = getattr(doc, "text", "")
        if "approv" not in text.lower() or "invoices/" in doc.file:
            continue
        for line in text.split("\n"):
            line = line.strip(" -*•\t")
            m_rest = re.search(r":\s*(?:approved by\s+)?(.+)$", line, re.I)
            if not m_rest or "approv" not in line.lower():
                continue
            approver = m_rest.group(1).rstrip(".")
            if m := re.search(rf"\bfrom\s+{num}\s+(?:up\s+)?to\s+{num}", line, re.I):
                rules.append(ApprovalRule(float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")), approver, doc.file))
            elif m := re.search(rf"\b(?:over|above|more than|exceeding)\s+{num}", line, re.I):
                rules.append(ApprovalRule(float(m.group(1).replace(",", "")), float("inf"), approver, doc.file))
            elif m := re.search(rf"\b(?:under|below|less than|up to)\s+{num}", line, re.I):
                rules.append(ApprovalRule(0.0, float(m.group(1).replace(",", "")), approver, doc.file))
    return rules


def recording_deadline_days(documents: list) -> tuple[int, str] | None:
    """"... recorded as bills ... within 5 business days of receipt" → (5, file)."""
    for doc in documents:
        m = re.search(r"recorded[^.]{0,120}?within\s+(\d+)\s+business\s+days", getattr(doc, "text", ""), re.I | re.S)
        if m and "invoices/" not in doc.file:
            return int(m.group(1)), doc.file
    return None


def _business_days(start: date, end: date) -> int:
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        days += d.weekday() < 5
    return days


# --------------------------------------------------------------------------- the list
def attention(store: DataStore, documents: list, as_of: date) -> dict[str, Any]:
    tables = set(store.tables())
    items: list[dict[str, Any]] = []

    # 1. invoices vs QuickBooks
    record_rule = recording_deadline_days(documents)
    currency_of: dict[str, str] = {}
    if "invoices" in tables:
        currency_of = {r["file"]: r["currency"] for r in query(store, "SELECT file, currency FROM invoices")
                       if r["file"] and r["currency"]}
    if "invoice_reconciliation" in tables:
        rec_cols = {c.lower() for c, _ in store.schema().get("invoice_reconciliation", [])}
        cur_col = "currency" if "currency" in rec_cols else "NULL AS currency"
        for r in query(store, "SELECT file, supplier, invoice_number, invoice_date, document_total, qbo_total, "
                              f"difference, {cur_col}, status, severity, detail FROM invoice_reconciliation "
                              "WHERE severity <> 'ok'"):
            label = _LABELS.get(r["status"], r["status"].replace("_", " "))
            who = f"{r['supplier'] or 'Unknown supplier'} {r['invoice_number'] or ''}".strip()
            amount = abs(r["difference"]) if r["difference"] else (r["document_total"] or r["qbo_total"])
            detail = r["detail"] or label
            sev = "critical" if r["severity"] == "issue" else "warning"
            if r["status"] == "not_in_quickbooks" and record_rule and r["invoice_date"]:
                late = _business_days(r["invoice_date"], as_of) - record_rule[0]
                if late > 0:
                    sev = "critical"
                    detail += (f" Policy: record within {record_rule[0]} business days of receipt; this is "
                               f"{late} business day(s) past that ({record_rule[1].rsplit('/', 1)[-1]}).")
            items.append(_item(sev, "reconciliation", f"{who}: {label}", detail,
                               {"type": "file" if r["file"] else "table", "name": r["file"] or "qbo_bills"},
                               amount, f"What should we do about {who}?", "quickbooks",
                               r["currency"] or currency_of.get(r["file"])))

    # 2. invoices awaiting review with problems, and the approval policy
    rules = approval_rules(documents)
    if "invoices" in tables:
        for r in query(store, "SELECT file, supplier, invoice_number, total, currency, status, issues, issue_count, "
                              "duplicate_of FROM invoices WHERE status = 'needs_review'"):
            cur = (r["currency"] or HOME_CURRENCY).upper()
            who = f"{r['supplier'] or 'Unknown supplier'} {r['invoice_number'] or '(no number)'}"
            if r["issue_count"] and not r["duplicate_of"]:
                first = (r["issues"] or "").split(" | ")[0]
                items.append(_item("warning", "review", f"Check invoice {who}", first,
                                   {"type": "file", "name": r["file"]}, r["total"],
                                   f"What is wrong with invoice {who}?", "invoices", cur))
            total = r["total"] or 0
            # the policy's thresholds are in the home currency: don't compare a foreign-currency total against them
            rule = None if cur != HOME_CURRENCY else next(
                (x for x in sorted(rules, key=lambda x: -x.low) if total > x.low and total <= x.high), None)
            if rule and rule.low > 0 and not r["duplicate_of"]:
                top_tier = rule.high == float("inf")  # the strictest rule is the one worth flagging
                items.append(_item("warning" if top_tier else "info", "policy",
                                   f"{who} needs approval by {rule.approver.split(',')[0]}",
                                   f"{_money(total)} is over {_money(rule.low)}. Policy: {rule.approver}.",
                                   {"type": "file", "name": rule.file}, total,
                                   f"Who has to approve invoice {who} under our policy?", "invoices"))

    # 3. overdue money
    for table, name_col, kind in (("qbo_invoices", "customer_name", "receivable"), ("qbo_bills", "vendor_name", "payable")):
        if table not in tables:
            continue
        for r in overdue_items(store, table, as_of, name_col):
            days = r["days_overdue"]
            sev = "critical" if days > 60 else "warning" if days > 14 else "info"
            if kind == "receivable":
                items.append(_item(sev, "receivables", f"{r['name']} owes {_money(r['balance'])}, {days} days overdue",
                                   f"Customer invoice {r['doc_number']} was due {r['due_date']}.",
                                   {"type": "table", "name": table}, r["balance"],
                                   f"Draft a payment reminder to {r['name']} for invoice {r['doc_number']}", "quickbooks"))
            else:
                items.append(_item(sev, "payables", f"Bill from {r['name']} is {days} days overdue",
                                   f"Bill {r['doc_number']} for {_money(r['balance'])} was due {r['due_date']}.",
                                   {"type": "table", "name": table}, r["balance"],
                                   f"Why is the {r['name']} bill {r['doc_number']} unpaid?", "quickbooks"))

    # 4. bank statement vs QuickBooks
    if "bank_reconciliation" in tables:
        for r in query(store, "SELECT date, description, amount, status, severity, detail, counterparty "
                              "FROM bank_reconciliation WHERE severity <> 'ok'"):
            sev = {"issue": "critical", "warning": "warning"}.get(r["severity"], "info")
            if r["status"] == "no_bill":
                sev = "info"  # payroll, utilities and bank fees normally have no supplier bill
            label = _LABELS.get(r["status"], r["status"].replace("_", " "))
            who = r["counterparty"] or r["description"] or "Bank line"
            items.append(_item(sev, "bank", f"{who}: {label}", f"{r['date']}: {r['detail'] or label}",
                               {"type": "table", "name": "bank_reconciliation"}, abs(r["amount"] or 0) or None,
                               f"Explain the bank line for {who}", "quickbooks"))

    # 5. budget
    for r in budget_vs_actual(store):
        budget, committed, spent = r.get("budget") or 0, r.get("committed") or 0, r.get("spent") or 0
        if budget and committed > budget:
            items.append(_item("critical", "budget", f"{r['item']} is over budget by {_money(committed - budget)}",
                               f"Committed {_money(committed)} against a budget of {_money(budget)} ({r['project']}).",
                               {"type": "table", "name": "project budget"}, committed - budget,
                               f"Why is {r['item']} over budget?", "overview"))
        elif budget and spent / budget >= 0.9:
            items.append(_item("info", "budget", f"{r['item']} has used {spent / budget:.0%} of its budget",
                               f"Spent {_money(spent)} of {_money(budget)} ({r['project']}).",
                               {"type": "table", "name": "project budget"}, None, None, "overview"))

    # 6. deadlines from the documents
    deadlines = find_deadlines(documents, as_of)
    for d in deadlines:
        if d.status not in ("overdue", "due_soon"):
            continue
        when = f"{-d.days} days ago" if d.days < 0 else ("today" if d.days == 0 else f"in {d.days} days")
        title = d.text if len(d.text) <= 110 else d.text[:107].rsplit(" ", 1)[0] + "…"
        items.append(_item("critical" if d.status == "overdue" else "warning", "deadline", title,
                           f"{'Was due' if d.days < 0 else 'Due'} {d.date} ({when}).",
                           {"type": "file", "name": d.file, "page": d.page}, None,
                           f"What is the status of: {d.text[:120]}", "documents"))

    # 7. anomalies across suppliers' history
    items += _anomalies(store, tables)

    items.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], -(i["amount"] or 0)))
    counts = {s: sum(1 for i in items if i["severity"] == s) for s in SEVERITY_ORDER}
    # money involved in problems (not in approvals or reviews, and each amount/source counted once)
    # (amounts in different currencies are kept apart: money_at_stake is the home currency, the rest per currency)
    seen: set[tuple[str, float, str]] = set()
    by_currency: dict[str, float] = {}
    for i in items:
        key = (i["source"].get("name", ""), i["amount"] or 0, i["currency"])
        if i["severity"] != "info" and i["category"] not in ("policy", "review") and i["amount"] and key not in seen:
            seen.add(key)
            by_currency[i["currency"]] = by_currency.get(i["currency"], 0.0) + i["amount"]
    at_stake = round(by_currency.pop(HOME_CURRENCY, 0.0), 2)
    other = {c: round(v, 2) for c, v in sorted(by_currency.items())}
    return {"as_of": as_of.isoformat(), "counts": counts, "currency": HOME_CURRENCY, "money_at_stake": at_stake,
            "money_at_stake_other": other, "items": items,
            "deadlines": [d.to_dict() for d in deadlines],
            "approval_rules": [{"over": r.low, "up_to": None if r.high == float("inf") else r.high,
                                "approver": r.approver, "file": r.file} for r in rules]}


def _anomalies(store: DataStore, tables: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "qbo_bills" not in tables:
        return out
    has_currency = "currency" in {c.lower() for c, _ in store.schema().get("qbo_bills", [])}
    bills = query(store, "SELECT vendor_name, doc_number, txn_date, total"
                         f"{', currency' if has_currency else ''} FROM qbo_bills WHERE total IS NOT NULL")
    by_vendor: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for b in bills:  # amounts are only compared within one supplier and one currency
        by_vendor.setdefault((b["vendor_name"] or "?", (b.get("currency") or HOME_CURRENCY).upper()), []).append(b)
    for (vendor, cur), rows in by_vendor.items():
        for b in rows:
            others = [o["total"] for o in rows if o is not b and o["total"]]
            if len(others) >= 2 and b["total"] > 2 * median(others):
                out.append(_item("warning", "anomaly", f"{vendor} bill {b['doc_number']} is unusually large",
                                 f"{money(b['total'], cur)} is more than twice this supplier's typical bill "
                                 f"({money(median(others), cur)}).", {"type": "table", "name": "qbo_bills"}, b["total"],
                                 f"Is the {vendor} bill {b['doc_number']} for {money(b['total'], cur)} correct?", "quickbooks",
                                 cur))
        seen: dict[float, dict[str, Any]] = {}
        for b in sorted(rows, key=lambda x: str(x["txn_date"])):
            prev = seen.get(round(b["total"], 2))
            if prev and prev["doc_number"] != b["doc_number"]:
                gap = abs((_as_date(b["txn_date"]) - _as_date(prev["txn_date"])).days) if b["txn_date"] and prev["txn_date"] else 0
                if gap <= 45:
                    out.append(_item("warning", "anomaly", f"{vendor} billed {money(b['total'], cur)} twice",
                                     f"Bills {prev['doc_number']} and {b['doc_number']} have the same amount, {gap} days "
                                     "apart. Check it isn't a double charge.", {"type": "table", "name": "qbo_bills"},
                                     b["total"], f"Did {vendor} charge us twice?", "quickbooks", cur))
            seen[round(b["total"], 2)] = b
    return out


def _as_date(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


# --------------------------------------------------------------------------- tool view for the model
def for_model(result: dict[str, Any], limit: int = 12) -> dict[str, Any]:
    """Compact version for the agent tool: the top items with their sources."""
    top = [{k: i[k] for k in ("severity", "category", "title", "detail", "amount", "currency", "source")}
           for i in result["items"][:limit]]
    return {"as_of": result["as_of"], "counts": result["counts"], "money_at_stake": result["money_at_stake"],
            "currency": result["currency"], "money_at_stake_other": result["money_at_stake_other"],
            "items": top, "note": "Cite the source of each item (file or table). Figures are drafts for review."}
