"""Retrieval: BM25, vector, and hybrid fusion surface the relevant chunk."""

from __future__ import annotations

from app.rag.retriever import Retriever


def test_hybrid_finds_relevant_chunk(retriever: Retriever):
    hits = retriever.search("what does baseline mean?", k=3)
    assert hits
    assert any("baseline" in h.text.lower() for h in hits)


def test_empty_query_safe(retriever: Retriever):
    # Should not raise even on a nonsense query.
    hits = retriever.search("zzzzzzz qqqqqq", k=3)
    assert isinstance(hits, list)


def test_doc_summary(retriever: Retriever):
    summary = retriever.doc_summary()
    assert "guide.md" in summary
