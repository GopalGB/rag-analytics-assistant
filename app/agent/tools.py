"""Tool schemas the model can call, and the ToolBox that executes them safely."""

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
]


class ToolBox:
    """Executes tool calls and records artifacts (SQL, rows, sources) for the final payload."""

    def __init__(self, store: DataStore, retriever: Retriever, max_rows: int = 200):
        self.store = store
        self.retriever = retriever
        self.max_rows = max_rows
        self.last_sql: str | None = None
        self.columns: list[str] = []
        self.rows: list[tuple] = []
        self.sources: list[dict[str, Any]] = []

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "run_sql":
            return self._run_sql(args.get("sql", ""))
        if name == "search_docs":
            return self._search_docs(args.get("query", ""), int(args.get("k", 5)))
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
            self.sources.append({"file": h.file, "chunk_id": h.chunk_id, "score": h.score})
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
