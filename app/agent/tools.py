"""Tool schemas the model can call, and the ToolBox that executes them safely.

- run_sql         read-only SELECT over the local tables (spreadsheets, extracted invoices, QuickBooks copy)
- search_docs     hybrid retrieval over documents; every hit carries file + page for citation
- propose_action  queue an external action (email, QuickBooks entry, task) for HUMAN approval — the
                  model can never execute anything itself
"""

from __future__ import annotations

from typing import Any

from app.data.store import DataStore, UnsafeQueryError
from app.rag.retriever import Retriever

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
            "name": "propose_action",
            "description": (
                "Queue an external action (e.g. sending an email, recording a bill in QuickBooks, a "
                "follow-up task) for a person to approve. Nothing is executed; use this only when the "
                "user asks for such an action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["draft_email", "record_bill", "follow_up_task", "other"],
                    },
                    "title": {"type": "string", "description": "Short summary of the action."},
                    "details": {"type": "string", "description": "Full draft / details for the approver."},
                },
                "required": ["action_type", "title", "details"],
            },
        },
    },
]


class ToolBox:
    """Executes tool calls and records artifacts (SQL, rows, sources) for the final payload."""

    def __init__(self, store: DataStore, retriever: Retriever, max_rows: int = 200, approvals: Any = None):
        self.store = store
        self.retriever = retriever
        self.max_rows = max_rows
        self.approvals = approvals
        self.last_sql: str | None = None
        self.columns: list[str] = []
        self.rows: list[tuple] = []
        self.sources: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "run_sql":
            return self._run_sql(args.get("sql", ""))
        if name == "search_docs":
            try:
                k = int(args.get("k", 5))
            except (TypeError, ValueError):
                k = 5
            return self._search_docs(str(args.get("query", "")), k)
        if name == "propose_action":
            return self._propose_action(args)
        return {"error": f"unknown tool: {name}"}

    def _run_sql(self, sql: str) -> dict[str, Any]:
        self.last_sql = sql
        try:
            columns, rows = self.store.run_select(sql, max_rows=self.max_rows)
        except UnsafeQueryError as exc:
            return {"error": f"unsafe query rejected: {exc}"}
        except Exception as exc:  # surface DB errors to the model so it can retry
            return {"error": f"query failed: {exc}"}
        self.columns, self.rows = columns, rows
        preview = [dict(zip(columns, r, strict=False)) for r in rows[:50]]
        return {"columns": columns, "row_count": len(rows), "rows": preview}

    def _search_docs(self, query: str, k: int) -> dict[str, Any]:
        hits = self.retriever.search(query, k=max(1, min(k, 10)))
        for h in hits:
            self.add_source(h)
        return {
            "results": [
                {"source": cite(h.file, h.page), "file": h.file, "page": h.page, "score": h.score, "text": h.text}
                for h in hits
            ]
        }

    def add_source(self, hit: Any) -> None:
        if any(s["file"] == hit.file and s["chunk_id"] == hit.chunk_id for s in self.sources):
            return
        self.sources.append(
            {
                "file": hit.file,
                "page": hit.page,
                "chunk_id": hit.chunk_id,
                "score": hit.score,
                "cite": cite(hit.file, hit.page),
                "snippet": hit.text[:320],
            }
        )

    def _propose_action(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.approvals is None:
            return {"error": "approvals are not available"}
        item = self.approvals.propose(
            str(args.get("action_type", "other")),
            str(args.get("title", "")),
            str(args.get("details", "")),
            proposed_by="assistant",
        )
        self.actions.append(item)
        return {"status": "queued_for_human_approval", "id": item["id"], "note": "Nothing has been sent or changed."}


def cite(file: str, page: int | None) -> str:
    name = file.rsplit("/", 1)[-1]
    return f"{name}, p.{page}" if page else name
