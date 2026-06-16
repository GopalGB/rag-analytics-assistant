"""Hybrid retriever: fuse BM25 (lexical) and vector (semantic) hits via reciprocal-rank fusion."""

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


class Retriever:
    """Holds the corpus and both indexes. Rebuilt on ingest/refresh."""

    def __init__(self, embeddings: EmbeddingService):
        self.embeddings = embeddings
        self.chunks: list[Chunk] = []
        self.bm25 = BM25()
        self.vectors = VectorIndex()

    def build(self, chunks: list[Chunk]) -> Retriever:
        self.chunks = chunks
        corpus = [c.text for c in chunks]
        self.bm25.fit(corpus)
        self.vectors.build(self.embeddings.embed(corpus))
        return self

    def search(self, query: str, k: int = 5, rrf_k: int = 60) -> list[Chunk]:
        if not self.chunks:
            return []
        lexical = self.bm25.search(query, k=k * 2)
        qvec = self.embeddings.embed([query])[0]
        semantic = self.vectors.search(qvec, k=k * 2)

        fused: dict[int, float] = {}
        for rank, (idx, _) in enumerate(lexical):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, (idx, _) in enumerate(semantic):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + rank + 1)

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:k]
        out: list[Chunk] = []
        for idx, score in ranked:
            c = self.chunks[idx]
            out.append(
                Chunk(
                    file=c.file, chunk_id=c.chunk_id, text=c.text, score=round(score, 5)
                )
            )
        return out

    def doc_summary(self, max_files: int = 20) -> str:
        files = sorted({c.file for c in self.chunks})
        head = ", ".join(files[:max_files])
        more = "" if len(files) <= max_files else f" (+{len(files) - max_files} more)"
        return f"{len(self.chunks)} chunks across files: {head}{more}" if files else ""
