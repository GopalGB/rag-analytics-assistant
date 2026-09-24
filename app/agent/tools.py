"""Tools the model can call, and the ToolBox that executes them safely.

- run_sql         read-only SELECT over the local tables (spreadsheets, extracted invoices, QuickBooks copy)
- search_docs     hybrid retrieval over documents; every hit carries file + page for citation
- propose_action  queue an external action (email, QuickBooks entry, task) for HUMAN approval — the
                  model can never execute anything itself

Type-safe: tool schemas are generated from the Pydantic models in `app.llm.schemas`, and every call's
arguments are validated against the same models before anything runs; invalid calls are answered with
the validation error so the model can correct itself.

Per request the ToolBox is restricted to the pipeline's tools (task router) and, while a cloud model is
active, to the data classes the privacy policy allows (privacy guard).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.data.store import DataStore, UnsafeQueryError
from app.llm.privacy import PrivacyGuard
from app.llm.schemas import TOOL_ARGS, ProposeActionArgs, RunSqlArgs, SearchDocsArgs, tool_spec
from app.rag.retriever import Retriever

ALL_TOOLS = tuple(TOOL_ARGS)
TOOLS: list[dict[str, Any]] = [tool_spec(n) for n in ALL_TOOLS]  # kept for callers of the old constant


class ToolBox:
    """Executes tool calls and records artifacts (SQL, rows, sources, actions) for the final payload."""

    def __init__(
        self,
        store: DataStore,
        retriever: Retriever,
        max_rows: int = 200,
        approvals: Any = None,
        allowed_tools: tuple[str, ...] | None = None,
        privacy: PrivacyGuard | None = None,
    ):
        self.store = store
        self.retriever = retriever
        self.max_rows = max_rows
        self.approvals = approvals
        self.allowed_tools = tuple(t for t in (allowed_tools or ALL_TOOLS) if t in TOOL_ARGS)
        if approvals is None:
            self.allowed_tools = tuple(t for t in self.allowed_tools if t != "propose_action")
        self.privacy = privacy
        self.last_sql: str | None = None
        self.columns: list[str] = []
        self.rows: list[tuple] = []
        self.sources: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []  # tool-call log for the trace
        self.listener: Any = None  # optional callback(kind, data) for live progress (streaming)

    def tool_specs(self) -> list[dict[str, Any]]:
        return [tool_spec(n) for n in self.allowed_tools]

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.listener:
            self.listener("tool", {"tool": name, "state": "start"})
        result = self._run(name, args)
        if self.listener:
            self.listener("tool", {"tool": name, "state": "done", "ok": "error" not in result})
        return result

    def _run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_ARGS:
            return self._log(name, {"error": f"unknown tool: {name}"})
        if name not in self.allowed_tools:
            return self._log(name, {"error": f"tool '{name}' is not available for this request"})
        try:
            parsed = TOOL_ARGS[name].model_validate(args or {})
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return self._log(name, {"error": f"invalid arguments: {errs}. Fix them and call again."})
        if isinstance(parsed, RunSqlArgs):
            return self._log(name, self._run_sql(parsed.sql))
        if isinstance(parsed, SearchDocsArgs):
            return self._log(name, self._search_docs(parsed.query, parsed.k))
        return self._log(name, self._propose_action(parsed))

    def _log(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"tool": name, "ok": "error" not in result, "error": result.get("error")})
        return result

    def _run_sql(self, sql: str) -> dict[str, Any]:
        self.last_sql = sql
        if self.privacy and self.privacy.cloud:
            try:
                tables = self.store.referenced_tables(sql)
            except UnsafeQueryError as exc:
                return {"error": f"unsafe query rejected: {exc}"}
            blocked = sorted(t for t in tables if not self.privacy.table_allowed(t))
            if blocked:
                return {"error": f"blocked by privacy policy: {', '.join(blocked)} must stay on this machine and "
                                 "cannot be sent to a cloud model. Say that this needs the local model."}
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
        hits = self.retriever.search(query, k=k)
        results = []
        for h in hits:
            if self.privacy and not self.privacy.file_allowed(h.file):
                self.privacy.withheld += 1
                continue
            self.add_source(h)
            text = self.privacy.outgoing(h.text) if self.privacy else h.text
            results.append({"source": cite(h.file, h.page), "file": h.file, "page": h.page, "score": h.score, "text": text})
        out: dict[str, Any] = {"results": results}
        if len(results) < len(hits):
            out["note"] = f"{len(hits) - len(results)} passage(s) withheld: that data must stay on this machine."
        return out

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

    def _propose_action(self, args: ProposeActionArgs) -> dict[str, Any]:
        item = self.approvals.propose(args.action_type, args.title, args.details, proposed_by="assistant")
        self.actions.append(item)
        return {"status": "queued_for_human_approval", "id": item["id"], "note": "Nothing has been sent or changed."}


def cite(file: str, page: int | None) -> str:
    name = file.rsplit("/", 1)[-1]
    return f"{name}, p.{page}" if page else name
