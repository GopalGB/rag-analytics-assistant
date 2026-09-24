"""Tool schemas the model can call, and the ToolBox that executes them safely."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.store import DataStore, UnsafeQueryError
from app.data.textindex import tokenize
from app.rag.retriever import Retriever

_INVOICE_FIELDS = ("supplier", "invoice_number", "date", "amount", "currency")
_SUPPORTED_CURRENCIES = {"AED", "USD", "EUR", "GBP"}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run ONE read-only DuckDB SELECT over the available tables and return rows. "
                "No DDL/DML, no file-reading functions, single statement only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single DuckDB SELECT statement.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Retrieve the most relevant document passages for a natural-language query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for in the documents.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "How many passages (default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "invoice_records",
            "description": "Return deterministic extracted invoice fields and review flags with source files.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolBox:
    """Executes tool calls and records artifacts (SQL, rows, sources) for the final payload."""

    def __init__(self, store: DataStore, retriever: Retriever, max_rows: int = 200, invoice_records: list[dict] | None = None):
        self.store = store
        self.retriever = retriever
        self.max_rows = max_rows
        self.last_sql: str | None = None
        self.columns: list[str] = []
        self.rows: list[tuple] = []
        self.sources: list[dict[str, Any]] = []
        self.timings_ms = {"retrieval": 0.0, "sql": 0.0}
        self._source_ids: set[str] = set()
        self.invoice_records = invoice_records or []

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            return {"error": "tool arguments must be an object"}
        if name == "run_sql":
            sql = args.get("sql")
            return self._run_sql(sql) if isinstance(sql, str) else {"error": "sql must be a string"}
        if name == "search_docs":
            query, k = args.get("query"), args.get("k", 5)
            if not isinstance(query, str) or not query.strip():
                return {"error": "query must be a non-empty string"}
            if not isinstance(k, int) or isinstance(k, bool):
                return {"error": "k must be an integer"}
            return self._search_docs(query, k)
        if name == "invoice_records":
            return self._invoice_records()
        return {"error": f"unknown tool: {name}"}

    def _run_sql(self, sql: str) -> dict[str, Any]:
        if not sql.strip():
            return {"error": "sql must be a non-empty string"}
        started = time.perf_counter()
        try:
            columns, rows = self.store.run_select(sql, max_rows=self.max_rows)
        except UnsafeQueryError as exc:
            return {"error": f"unsafe query rejected: {exc}"}
        except Exception as exc:  # surface DB errors to the model so it can retry
            return {"error": f"query failed: {exc}"}
        self.timings_ms["sql"] += round((time.perf_counter() - started) * 1000, 2)
        self.last_sql = sql
        self.columns, self.rows = columns, rows
        for table, source_file in self.store.source_tables(sql):
            self._add_source(
                {
                    "source_id": f"table:{table}",
                    "file": source_file,
                    "table": table,
                    "excerpt": f"SQL query executed against CSV table {table}.",
                    "text": f"SQL query executed against CSV table {table}.",
                }
            )
        preview = [dict(zip(columns, r, strict=False)) for r in rows[:50]]
        return {"columns": columns, "row_count": len(rows), "rows": preview}

    def _search_docs(self, query: str, k: int) -> dict[str, Any]:
        started = time.perf_counter()
        hits = self.retriever.search(query, k=max(1, min(k, 10)))
        terms = {term for term in tokenize(query) if len(term) > 2}
        hits = [hit for hit in hits if terms & set(tokenize(hit.text))] if terms else []
        self.timings_ms["retrieval"] += round((time.perf_counter() - started) * 1000, 2)
        for h in hits:
            excerpt = " ".join(h.text.split())[:500]
            self._add_source(
                {
                    "source_id": f"{h.file}:{h.chunk_id}",
                    "file": h.file,
                    "chunk_id": h.chunk_id,
                    "score": h.score,
                    "excerpt": excerpt,
                    "text": excerpt,
                }
            )
        return {
            "results": [
                {
                    "file": h.file,
                    "chunk_id": h.chunk_id,
                    "score": h.score,
                    "text": h.text,
                }
                for h in hits
            ]
        }

    def _invoice_records(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        totals: dict[str, Decimal] = {}
        included: dict[str, list[str]] = {}
        excluded: list[dict[str, Any]] = []
        source_records = sorted(self.invoice_records, key=lambda item: str(item.get("source_file", "")))
        for record in source_records:
            source_file = record.get("source_file")
            if not isinstance(source_file, str) or not source_file:
                continue
            compact = self._compact_invoice_record(record, source_file)
            records.append(compact)
            reasons, amount = self._invoice_exclusion(compact)
            currency = compact["currency"]
            if reasons:
                excluded.append({"source_file": source_file, "reasons": reasons})
            else:
                totals[currency] = totals.get(currency, Decimal("0")) + amount
                included.setdefault(currency, []).append(source_file)
            excerpt = "; ".join(
                f"{field}={compact[field]}" for field in ("supplier", "amount", "currency") if compact[field]
            )
            self._add_source(
                {
                    "source_id": f"invoice:{source_file}",
                    "file": source_file,
                    "excerpt": f"Invoice extraction: {excerpt}.",
                    "text": f"Invoice extraction: {excerpt}.",
                }
            )
        return {
            "records": records,
            "totals_by_currency": {currency: f"{totals[currency]:.2f}" for currency in sorted(totals)},
            "included_sources": {currency: included[currency] for currency in sorted(included)},
            "excluded_sources": excluded,
        }

    @staticmethod
    def _compact_invoice_record(record: dict[str, Any], source_file: str) -> dict[str, Any]:
        compact = {field: record[field] if isinstance(record.get(field), str) else None for field in _INVOICE_FIELDS}
        compact["source_file"] = source_file
        compact["missing_fields"] = sorted(field for field in record.get("missing_fields", []) if isinstance(field, str))
        compact["review_fields"] = sorted(field for field in record.get("review_fields", []) if isinstance(field, str))
        return {"source_file": compact.pop("source_file"), **compact}

    @staticmethod
    def _invoice_exclusion(record: dict[str, Any]) -> tuple[list[str], Decimal]:
        missing = set(record["missing_fields"])
        review = set(record["review_fields"])
        reasons = [f"missing: {field}" for field in ("amount", "currency") if field in missing]
        reasons.extend(f"review: {field}" for field in ("amount", "currency") if field in review)
        amount, currency = record["amount"], record["currency"]
        if not isinstance(amount, str) and "missing: amount" not in reasons:
            reasons.append("missing: amount")
        if not isinstance(currency, str) and "missing: currency" not in reasons:
            reasons.append("missing: currency")
        if isinstance(currency, str) and currency not in _SUPPORTED_CURRENCIES:
            reasons.append(f"unknown currency: {currency}")
        try:
            parsed_amount = Decimal(amount) if isinstance(amount, str) else Decimal("0")
            if not parsed_amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            if "invalid amount" not in reasons:
                reasons.append("invalid amount")
            parsed_amount = Decimal("0")
        return reasons, parsed_amount

    def _add_source(self, source: dict[str, Any]) -> None:
        source_id = source["source_id"]
        if source_id not in self._source_ids:
            self._source_ids.add(source_id)
            self.sources.append(source)
