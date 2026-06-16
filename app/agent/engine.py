"""AgentEngine — wires guardrail → LLM tool-loop → output scrub.

This is an LLM-first assistant: every analytical answer comes from a live model running its native
tool-calling loop. There is no deterministic "offline answerer" — if no LLM is configured the engine
returns an honest "configure a provider" message rather than faking an answer. Startup normally fails
fast (see `require_llm`), so this path is only reachable when the operator explicitly opts out.
"""

from __future__ import annotations

from typing import Any

from app.agent.llm import BaseLLM
from app.agent.memory import ConversationMemory
from app.agent.tools import ToolBox
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

        if self.llm is None:
            return {
                "text": (
                    "No language model is configured, so I can't answer. This assistant is "
                    "LLM-first — set one of OPENAI_API_KEY, BEDROCK_MODEL_ID, or LLM_CLI_COMMAND "
                    "(e.g. the ChatGPT/Codex CLI) and restart."
                ),
                "route": "no_llm",
                "sql": None,
                "sources": [],
            }

        payload = self._agentic(session_id, question)
        payload["text"] = scrub(payload.get("text", ""))
        self.memory.add(session_id, "user", question)
        self.memory.add(session_id, "assistant", payload["text"])
        return payload

    def _agentic(self, session_id: str, question: str) -> dict[str, Any]:
        toolbox = ToolBox(self.store, self.retriever, max_rows=self.max_sql_rows)
        system = build_system_prompt(self.store.schema_summary(), self.retriever.doc_summary())
        # The provider runs its own native tool-calling loop and records artifacts on the toolbox.
        final_text = self.llm.converse(
            system=system,
            history=self.memory.history(session_id),
            question=question,
            toolbox=toolbox,
            max_iters=self.max_tool_iterations,
        )

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
