"""Hybrid retriever: fuse BM25 (lexical) and vector (semantic) hits by weighted, normalised scores.

Each list is normalised to [0, 1] (divide by its best score) and combined as a weighted sum. Unlike
plain rank fusion this keeps the *margin* of a strong exact-term match, which matters for business
documents full of names and numbers. With the offline hashing embedder, lexical evidence is weighted
higher; with a real semantic embedding model the weights are balanced.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.data.textindex import BM25
from app.rag.embeddings import EmbeddingService
from app.rag.vectors import VectorIndex


@dataclass
class Chunk:
    file: str
    chunk_id: int
    text: str
    score: float = 0.0
    page: int | None = None  # 1-based page for paged documents (PDF); None otherwise


class Retriever:
    """Holds the corpus and both indexes. Rebuilt on ingest/refresh."""

    def __init__(self, embeddings: EmbeddingService, lexical_weight: float | None = None):
        self.embeddings = embeddings
        if lexical_weight is None:
            lexical_weight = 0.7 if embeddings.provider == "local" else 0.5
        self.lexical_weight = lexical_weight
        self.chunks: list[Chunk] = []
        self.bm25 = BM25()
        self.vectors = VectorIndex()

    def build(self, chunks: list[Chunk]) -> Retriever:
        self.chunks = chunks
        corpus = [c.text for c in chunks]
        self.bm25.fit(corpus)
        self.vectors.build(self.embeddings.embed(corpus))
        return self

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        if not self.chunks:
            return []
        lexical = self.bm25.search(query, k=k * 3)
        qvec = self.embeddings.embed([query])[0]
        semantic = [(i, s) for i, s in self.vectors.search(qvec, k=k * 3) if s > 0]

        fused: dict[int, float] = {}
        for hits, weight in ((lexical, self.lexical_weight), (semantic, 1.0 - self.lexical_weight)):
            top = hits[0][1] if hits else 0.0
            for idx, score in hits:
                fused[idx] = fused.get(idx, 0.0) + weight * (score / top if top else 0.0)

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]
        out: list[Chunk] = []
        for idx, score in ranked:
            c = self.chunks[idx]
            out.append(
                Chunk(file=c.file, chunk_id=c.chunk_id, text=c.text, score=round(score, 5), page=c.page)
            )
        return out

    def doc_summary(self, max_files: int = 20) -> str:
        files = sorted({c.file for c in self.chunks})
        head = ", ".join(files[:max_files])
        more = "" if len(files) <= max_files else f" (+{len(files) - max_files} more)"
        return f"{len(self.chunks)} chunks across files: {head}{more}" if files else ""
