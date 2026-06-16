"""AgentEngine — wires guardrail → (LLM tool-loop | deterministic fallback) → output scrub."""

from __future__ import annotations

import json
from typing import Any

from app.agent import fallback
from app.agent.llm import BaseLLM
from app.agent.memory import ConversationMemory
from app.agent.tools import TOOLS, ToolBox
from app.data.store import DataStore
from app.rag.retriever import Retriever
from app.security import InputGuard, build_system_prompt, scrub


class AgentEngine:
    def __init__(
        self,
        store: DataStore,
        retriever: Retriever,
        guard: InputGuard,
        llm: BaseLLM | None,
        memory: ConversationMemory,
        max_tool_iterations: int = 4,
        max_sql_rows: int = 200,
    ):
        self.store = store
        self.retriever = retriever
        self.guard = guard
        self.llm = llm
        self.memory = memory
        self.max_tool_iterations = max_tool_iterations
        self.max_sql_rows = max_sql_rows

    def status(self) -> dict[str, Any]:
        return {
            "llm_enabled": self.llm is not None,
            "embedding": self.retriever.embeddings.name,
            "tables": self.store.tables(),
            "doc_chunks": len(self.retriever.chunks),
        }

    def answer(self, session_id: str, question: str) -> dict[str, Any]:
        decision = self.guard.inspect(question)
        if decision.category == "greeting":
            return {
                "text": decision.user_message,
                "route": "greeting",
                "sql": None,
                "sources": [],
            }
        if not decision.allowed:
            return {
                "text": decision.user_message,
                "route": "refused",
                "category": decision.category,
                "sql": None,
                "sources": [],
            }

        if self.llm is not None:
            payload = self._agentic(session_id, question)
        else:
            payload = fallback.answer(question, self.store, self.retriever)

        payload["text"] = scrub(payload.get("text", ""))
        self.memory.add(session_id, "user", question)
        self.memory.add(session_id, "assistant", payload["text"])
        return payload

    def _agentic(self, session_id: str, question: str) -> dict[str, Any]:
        toolbox = ToolBox(self.store, self.retriever, max_rows=self.max_sql_rows)
        system = build_system_prompt(self.store.schema_summary(), self.retriever.doc_summary())
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(self.memory.history(session_id))
        messages.append({"role": "user", "content": question})

        final_text = ""
        for _ in range(self.max_tool_iterations):
            resp = self.llm.chat(messages, TOOLS)
            if not resp.tool_calls:
                final_text = resp.content or ""
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in resp.tool_calls
                    ],
                }
            )
            for tc in resp.tool_calls:
                result = toolbox.run(tc.name, tc.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result)[:8000],
                    }
                )
        else:
            # Tool budget exhausted — force a final answer from gathered evidence, no more tools.
            resp = self.llm.chat(
                messages
                + [
                    {
                        "role": "user",
                        "content": "Give your best final answer now from the evidence gathered.",
                    }
                ],
                [],
            )
            final_text = resp.content or final_text

        table_preview = [dict(zip(toolbox.columns, r, strict=False)) for r in toolbox.rows[:100]]
        return {
            "text": final_text,
            "route": "agent",
            "sql": toolbox.last_sql,
            "columns": toolbox.columns,
            "rows": table_preview,
            "row_count": len(toolbox.rows),
            "sources": toolbox.sources,
        }
