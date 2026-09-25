"""Tools the model can call, and the ToolBox that executes them safely.

- run_sql         read-only SELECT over the local tables (spreadsheets, extracted invoices, QuickBooks copy)
- search_docs     hybrid retrieval over documents; every hit carries file + page for citation
- propose_action  queue an external action (email, QuickBooks entry, task) for HUMAN approval — the
                  model can never execute anything itself
- attention_items the ranked "needs attention" list (accounting, bank, budget, deadlines, policy), with sources

Type-safe: tool schemas are generated from the Pydantic models in `app.llm.schemas`, and every call's
arguments are validated against the same models before anything runs; invalid calls are answered with
the validation error so the model can correct itself.

Per request the ToolBox is restricted to the pipeline's tools (task router) and, while a cloud model is
active, to the data classes the privacy policy allows (privacy guard).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.data.store import DataStore, UnsafeQueryError
from app.llm.privacy import PrivacyGuard
from app.llm.schemas import (
    TOOL_ARGS,
    TOOL_RESULT_CHARS,
    AttentionArgs,
    ProposeActionArgs,
    RunSqlArgs,
    SearchDocsArgs,
    tool_spec,
)
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
        insights: Any = None,
    ):
        self.store = store
        self.retriever = retriever
        self.max_rows = max_rows
        self.approvals = approvals
        self.allowed_tools = tuple(t for t in (allowed_tools or ALL_TOOLS) if t in TOOL_ARGS)
        if approvals is None:
            self.allowed_tools = tuple(t for t in self.allowed_tools if t != "propose_action")
        self.insights = insights  # callable returning the attention list; the tool is offered only with it
        if insights is None:
            self.allowed_tools = tuple(t for t in self.allowed_tools if t != "attention_items")
        self.privacy = privacy
        self.last_sql: str | None = None
        self.columns: list[str] = []
        self.rows: list[tuple] = []
        self.sources: list[dict[str, Any]] = []
        # Passages the model already has (pre-fetched into the prompt or returned earlier): a repeat search
        # references them instead of re-sending the text, since every tool turn resends the conversation.
        self.shown: set[tuple[str, Any]] = set()
        self.actions: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []  # tool-call log for the trace
        self.listener: Any = None  # optional callback(kind, data) for live progress (streaming)
        self.touched_classes: set[str] = set()  # data classes of every table read and passage returned

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
        if isinstance(parsed, AttentionArgs):
            return self._log(name, self._attention(parsed.limit))
        return self._log(name, self._propose_action(parsed))

    def _attention(self, limit: int) -> dict[str, Any]:
        from app.accounting.insights import for_model

        blocked = {"error": "blocked by privacy policy: the attention list includes accounting and bank data, which "
                            "must stay on this machine. Say that this needs the local model."}
        if self.privacy and self.privacy.cloud and not self.privacy.policy.cloud_ok_for("accounting"):
            return blocked
        result = self.insights()
        # every item's own source must be allowed too (bank lines, invoice files, personal documents ...)
        classes = {"accounting"}
        for item in result["items"]:
            src = item.get("source") or {}
            if src.get("type") == "file":
                classes.add(self.privacy.policy.classify_file(src.get("name", "")) if self.privacy else "documents")
                allowed = not self.privacy or self.privacy.file_allowed(src.get("name", ""))
            elif src.get("type") == "table":
                classes.add(self.privacy.policy.classify_table(src.get("name", "")) if self.privacy else "accounting")
                allowed = not self.privacy or self.privacy.table_allowed(src.get("name", ""))
            else:
                allowed = not (self.privacy and self.privacy.cloud)  # unknown source: fail closed for cloud models
            if not allowed:
                return blocked
        self.touched_classes |= classes
        view = for_model(result, limit)
        if self.privacy:  # same PII masking as document passages before anything goes to a cloud model
            for item in view["items"]:
                item["title"] = self.privacy.outgoing(item["title"])
                item["detail"] = self.privacy.outgoing(item["detail"])
        return view

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
        self._touch_tables(sql)
        self.columns, self.rows = columns, rows
        preview = [dict(zip(columns, r, strict=False)) for r in rows[:50]]
        return {"columns": columns, "row_count": len(rows), "rows": preview}

    def _search_docs(self, query: str, k: int) -> dict[str, Any]:
        hits = self.retriever.search(query, k=k)
        results = []
        repeats = 0
        size = len('{"results": []}')
        for h in hits:
            if self.privacy and not self.privacy.file_allowed(h.file):
                self.privacy.withheld += 1
                continue
            self.add_source(h)
            key = (h.file, h.chunk_id)
            seen_before = key in self.shown
            if seen_before:
                repeats += 1
                text = "(already shown above)"
            else:
                text = self.privacy.outgoing(h.text) if self.privacy else h.text
            entry = {"source": cite(h.file, h.page), "file": h.file, "page": h.page, "score": h.score, "text": text}
            size += len(json.dumps(entry, default=str)) + 2
            if not seen_before and size <= TOOL_RESULT_CHARS - 300:  # only what survives the cut reaches the model
                self.shown.add(key)
            results.append(entry)
        out: dict[str, Any] = {"results": results}
        if repeats:
            out["note"] = (f"{repeats} passage(s) were already shown above: answer from them, or search with "
                           "different words.")
        if len(results) < len(hits):
            withheld = f"{len(hits) - len(results)} passage(s) withheld: that data must stay on this machine."
            out["note"] = f"{out['note']} {withheld}" if "note" in out else withheld
        return out

    def _touch_tables(self, sql: str) -> None:
        if not self.privacy:
            return
        try:
            tables = self.store.referenced_tables(sql)
        except UnsafeQueryError:
            return
        self.touched_classes |= {self.privacy.policy.classify_table(t) for t in tables}

    def add_source(self, hit: Any) -> None:
        if self.privacy:
            self.touched_classes.add(self.privacy.policy.classify_file(hit.file))
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
