"""Deterministic answerer used when no LLM provider is configured.

Keeps the app useful offline: schema questions and document lookups work without a model.
Analytical free-form questions return a clear, honest "connect a model" message — never a guess.
"""

from __future__ import annotations

import re
from typing import Any

from app.data.store import DataStore
from app.rag.retriever import Retriever

_SCHEMA_RE = re.compile(
    r"\b(schema|columns?|tables?|fields?|what.*data|structure)\b", re.I
)


def answer(
    question: str, store: DataStore, retriever: Retriever, max_chunks: int = 4
) -> dict[str, Any]:
    if _SCHEMA_RE.search(question):
        summary = store.schema_summary()
        text = "Here are the available tables and columns:\n\n" + (
            summary or "(no tables loaded)"
        )
        return {"text": text, "route": "fallback:schema", "sql": None, "sources": []}

    hits = retriever.search(question, k=max_chunks)
    if hits:
        joined = "\n\n".join(f"[{h.file}#{h.chunk_id}] {h.text}" for h in hits)
        text = (
            "No language model is configured, so here are the most relevant passages I found. "
            "Connect an LLM (set OPENAI_API_KEY) for synthesized answers.\n\n" + joined
        )
        sources = [
            {"file": h.file, "chunk_id": h.chunk_id, "score": h.score} for h in hits
        ]
        return {"text": text, "route": "fallback:rag", "sql": None, "sources": sources}

    return {
        "text": (
            "I can't answer analytical questions without a language model configured. "
            "Set OPENAI_API_KEY to enable SQL reasoning and synthesis, or ask me about the "
            "data schema / documents, which work offline."
        ),
        "route": "fallback:guidance",
        "sql": None,
        "sources": [],
    }
