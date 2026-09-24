"""Pull read-only QuickBooks data into local DuckDB tables the assistant can query.

Tables: qbo_company, qbo_vendors, qbo_customers, qbo_bills, qbo_invoices (sales / receivables),
qbo_accounts. Data is copied locally so questions never trigger live calls; re-sync to refresh.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.data.store import DataStore
from app.integrations.quickbooks import QuickBooksClient


def _ref(row: dict[str, Any], key: str) -> tuple[str | None, str | None]:
    ref = row.get(key) or {}
    return ref.get("value"), ref.get("name")


def _days_overdue(due: str | None, balance: float, today: date) -> int:
    if not due or not balance:
        return 0
    try:
        return max(0, (today - date.fromisoformat(due)).days)
    except ValueError:
        return 0


def sync(client: QuickBooksClient, store: DataStore, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    company = client.query("select * from CompanyInfo")
    vendors = client.query("select * from Vendor")
    customers = client.query("select * from Customer")
    bills = client.query("select * from Bill")
    invoices = client.query("select * from Invoice")
    accounts = client.query("select * from Account")

    frames: dict[str, pd.DataFrame] = {
        "qbo_company": pd.DataFrame(
            [{"id": c.get("Id"), "company_name": c.get("CompanyName"), "country": c.get("Country")} for c in company]
        ),
        "qbo_vendors": pd.DataFrame(
            [
                {"id": v.get("Id"), "name": v.get("DisplayName"), "active": v.get("Active"), "balance": v.get("Balance")}
                for v in vendors
            ]
        ),
        "qbo_customers": pd.DataFrame(
            [
                {"id": c.get("Id"), "name": c.get("DisplayName"), "active": c.get("Active"), "balance": c.get("Balance")}
                for c in customers
            ]
        ),
        "qbo_bills": pd.DataFrame(
            [
                {
                    "id": b.get("Id"),
                    "doc_number": b.get("DocNumber"),
                    "vendor_id": _ref(b, "VendorRef")[0],
                    "vendor_name": _ref(b, "VendorRef")[1],
                    "txn_date": b.get("TxnDate"),
                    "due_date": b.get("DueDate"),
                    "total": b.get("TotalAmt"),
                    "balance": b.get("Balance"),
                    "currency": _ref(b, "CurrencyRef")[0],
                    "status": "paid" if not b.get("Balance") else "open",
                    "days_overdue": _days_overdue(b.get("DueDate"), b.get("Balance") or 0, today),
                }
                for b in bills
            ]
        ),
        "qbo_invoices": pd.DataFrame(
            [
                {
                    "id": i.get("Id"),
                    "doc_number": i.get("DocNumber"),
                    "customer_id": _ref(i, "CustomerRef")[0],
                    "customer_name": _ref(i, "CustomerRef")[1],
                    "txn_date": i.get("TxnDate"),
                    "due_date": i.get("DueDate"),
                    "total": i.get("TotalAmt"),
                    "balance": i.get("Balance"),
                    "status": "paid" if not i.get("Balance") else "open",
                    "days_overdue": _days_overdue(i.get("DueDate"), i.get("Balance") or 0, today),
                }
                for i in invoices
            ]
        ),
        "qbo_accounts": pd.DataFrame(
            [
                {
                    "id": a.get("Id"),
                    "name": a.get("Name"),
                    "account_type": a.get("AccountType"),
                    "account_sub_type": a.get("AccountSubType"),
                    "current_balance": a.get("CurrentBalance"),
                    "active": a.get("Active"),
                }
                for a in accounts
            ]
        ),
    }
    for name in ("qbo_bills", "qbo_invoices"):
        df = frames[name]
        if not df.empty:
            for col in ("txn_date", "due_date"):
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    counts: dict[str, int] = {}
    for table, df in frames.items():
        if df.empty:
            continue
        counts[table] = store.load_dataframe(table, df)
    return counts


def vendor_names(client: QuickBooksClient) -> list[str]:
    try:
        return [v.get("DisplayName") for v in client.query("select * from Vendor") if v.get("DisplayName")]
    except Exception:
        return []
